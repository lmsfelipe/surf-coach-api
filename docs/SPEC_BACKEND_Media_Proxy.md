# SPEC — Backend Media Proxy / Streaming Endpoint

**Status:** Proposed
**Owner:** Backend
**Requested by:** Frontend (Surf Coach UI)
**Date:** 2026-06-05

---

## 1. Problem

Today the media API (`MediaOut`) returns a **raw Supabase Storage URL** in the
`storageUrl` field, e.g.:

```
https://lulfuyeclxlrxwmagozv.supabase.co/storage/v1/object/public/surf-media/<profile>/<session>/<file>.mp4
```

The frontend puts that string straight into `<img src>` / `<video src>`. Two problems:

1. **It's broken.** Requesting that URL returns:
   ```
   HTTP 400
   {"statusCode":"404","error":"Bucket not found","message":"Bucket not found"}
   ```
   The `surf-media` bucket is not publicly reachable in the Supabase project, so
   **no session media (images or video) loads in the browser**.

2. **It leaks infrastructure.** Even when fixed, exposing the Supabase host +
   bucket + object path publicly means anyone with the URL can read the object,
   and we can never make the bucket private without breaking the client.

## 2. Goals

- Frontend fetches media **from our backend only**. The Supabase host, bucket
  name, and object path are never sent to the client.
- The Supabase `surf-media` bucket can be **private**.
- Access is **authorized**: a user can only read media belonging to their own
  profile/session.
- **Video works**: the endpoint must support HTTP **Range** requests so the
  `<video>` element can stream and seek (this is required — without `206 Partial
  Content` Safari/iOS will not play, and scrubbing breaks everywhere).
- Works with plain `<img src>` / `<video src>` tags (see §4 on auth — these tags
  cannot send an `Authorization` header).

## 3. Non-goals

- Server-side transcoding or thumbnail/poster generation (possible later; see §9).
- Changing the upload flow (`POST /api/v1/sessions/{id}/media/` stays as is).
- CDN / caching layer (can be added later behind the same URL).

---

## 4. Design

### 4.1 New endpoint — stream media content

```
GET /api/v1/media/{media_id}/content
```

Streams the raw bytes of the object from Supabase Storage **through the backend**.
The backend reads from a (private) bucket using its service role and pipes the
body back to the client.

**The catch — auth on a bare media tag.** `<img>` and `<video>` cannot attach an
`Authorization: Bearer …` header. So this endpoint must authorize via something
the browser sends automatically with a tag request. Recommended:

> **A short-lived, signed access token in the query string**, minted by the
> backend and embedded in the URL we return to the client (see §4.2).

```
GET /api/v1/media/{media_id}/content?token=<signed-token>
```

The endpoint validates the token instead of (or in addition to) a Bearer header.

*(Alternative considered: cookie-based session auth. Rejected — the app uses
Supabase bearer tokens, not cookies, and a cookie would apply to every request.
A second alternative — the frontend `fetch()`es bytes with the Bearer header and
builds a `blob:` URL — is viable but loads the whole file into memory and
complicates range/seeking, so the signed-URL approach is preferred.)*

### 4.2 Changed response — replace `storageUrl` with a backend URL

`MediaOut` (returned by `GET /api/v1/media/{id}`,
`GET /api/v1/sessions/{id}/media/`, and the upload response) changes:

| Field | Before | After |
| --- | --- | --- |
| `storageUrl` | raw Supabase public URL | **removed** |
| `contentUrl` | — | backend URL incl. signed token: `/api/v1/media/{id}/content?token=…` |

`contentUrl` should be a path (or absolute URL on our API host) — **never** a
`*.supabase.co` URL.

The embedded token is scoped to **this `media_id` + the authenticated profile**
and is **short-lived** (suggested **15 min**; long enough to open + watch one
clip). The frontend re-fetches the `MediaOut` (cheap JSON) when a link expires,
so expiry is safe.

```jsonc
// MediaOut (after)
{
  "id": "….",
  "sessionId": "….",
  "mediaType": "video",
  "contentUrl": "/api/v1/media/0b2ec339…/content?token=eyJhbGciOi…",
  "fileName": "wave.mp4",
  "fileSizeBytes": 8123456,
  "durationSeconds": 41.2,
  "createdAt": "2026-06-04T12:00:00Z"
}
```

### 4.3 Token

A signed token (JWT or HMAC), opaque to the client. Suggested claims:

| Claim | Meaning |
| --- | --- |
| `media_id` | the only object this token can read |
| `sub` / `profile_id` | the profile it was issued to |
| `exp` | expiry (≈ now + 15 min) |
| `iat` | issued-at |

Validation on `…/content`:
- signature valid and not expired,
- `media_id` in token == `{media_id}` in path,
- the media row exists and belongs to `profile_id`.

No DB write needed (stateless). Signing key lives only on the backend.

### 4.4 Range / streaming behavior

The endpoint **must** honor `Range`:

| Request | Response |
| --- | --- |
| no `Range` header | `200 OK`, full body, `Accept-Ranges: bytes`, `Content-Length: <size>` |
| `Range: bytes=0-` | `206 Partial Content`, `Content-Range: bytes 0-<end>/<total>`, `Content-Length: <chunk>` |
| `Range: bytes=N-M` | `206`, the requested slice |
| unsatisfiable range | `416 Range Not Satisfiable` |

Supabase Storage's object API supports range reads — forward the client's
`Range` header to Supabase and relay its `206` + `Content-Range` back, or
re-derive them.

**Required response headers:**

- `Content-Type` — the media's real MIME type (e.g. `video/mp4`, `image/jpeg`).
  Do **not** return `application/json`.
- `Accept-Ranges: bytes`
- `Content-Length`
- `Content-Range` (on `206`)
- `Cache-Control: private, max-age=3600` (optional; tune as desired)

### 4.5 Status codes & errors

JSON errors use the existing envelope (`{ "error": { "code", "message", "details" } }`):

| Status | Code | When |
| --- | --- | --- |
| `200` / `206` | — | success (see §4.4) |
| `400` | `INVALID_TOKEN` | token malformed |
| `401` | `TOKEN_EXPIRED` | token expired |
| `403` | `FORBIDDEN` | token/media belongs to another profile |
| `404` | `MEDIA_NOT_FOUND` | no such media row |
| `416` | `RANGE_NOT_SATISFIABLE` | bad range |
| `502` | `STORAGE_ERROR` | upstream Supabase failure |

> Note: error **bodies** are JSON, but **success bodies are raw bytes** with the
> media `Content-Type` — never wrap success in JSON.

---

## 5. Sequence

```
Browser                         Backend                         Supabase (private)
  │  GET /sessions/{id}/media/    │                                │
  │ ─────────────────────────────▶ (Bearer)                       │
  │                               │  list rows, mint per-item token│
  │  ◀───────────────────────────  [{ contentUrl: …?token=… }]    │
  │                               │                                │
  │  <video src="…/content?token">│                                │
  │  GET /media/{id}/content      │                                │
  │  Range: bytes=0-              │                                │
  │ ─────────────────────────────▶ validate token, authz          │
  │                               │  GET object (Range) ──────────▶│
  │                               │  ◀──────────── 206 + bytes ────│
  │  ◀──────────── 206 + bytes ───                                 │
```

## 6. Security considerations

- Bucket `surf-media` becomes **private**; only the backend service role reads it.
- Tokens are short-lived, single-object, single-profile → a leaked URL grants
  read of one clip for ≤15 min, nothing else.
- Validate that the media row's `profile_id` matches the token's `profile_id`
  (defense in depth even though the token is media-scoped).
- Don't log full tokens.

## 7. Backwards compatibility / migration

`storageUrl → contentUrl` is a **breaking** response change. Coordinate:

- **Option A (clean):** ship `contentUrl`, remove `storageUrl`, frontend switches
  in the same release. Preferred — the field is only consumed in two components.
- **Option B (transitional):** return **both** for one release (`storageUrl`
  pointing at the new proxy too), then drop `storageUrl`.

Frontend consumers (we'll update these): `MediaThumb`, `MediaGallery`,
and the `Media` type in `src/types/api.ts` / `src/hooks/queries/media.ts`.

## 8. Acceptance criteria

- [ ] `GET /api/v1/media/{id}/content?token=…` returns `200` with the correct
      `Content-Type` and the full bytes when no `Range` is sent.
- [ ] Same endpoint returns `206 Partial Content` with valid `Content-Range` for
      `Range: bytes=0-`, and video **plays and seeks** in Chrome, Safari, iOS.
- [ ] Response never contains a `*.supabase.co` URL anywhere; `MediaOut` exposes
      only `contentUrl` (no `storageUrl`).
- [ ] A token for media A cannot read media B (`403`).
- [ ] An expired token returns `401 TOKEN_EXPIRED`.
- [ ] A user cannot read another profile's media (`403`/`404`).
- [ ] The `surf-media` bucket can be set to private and media still loads.

## 9. Future enhancements (out of scope)

- Server-generated **video poster/thumbnail** (`posterUrl` on `MediaOut`) so the
  grid shows a frame without downloading the clip.
- CDN edge caching keyed on the signed URL.
- `HEAD /api/v1/media/{id}/content` for size/type probing.

---

## Appendix — current contract (for reference)

```
GET    /api/v1/media/{media_id}            → MediaOut
DELETE /api/v1/media/{media_id}            → 204
GET    /api/v1/sessions/{session_id}/media/ → MediaOut[]   (note: trailing slash)
POST   /api/v1/sessions/{session_id}/media/ → MediaOut[]   (multipart, field `file`)
```

```jsonc
// MediaOut (current)
{
  "id": "uuid",
  "sessionId": "uuid",
  "mediaType": "image" | "video",
  "storageUrl": "https://<proj>.supabase.co/storage/v1/object/public/surf-media/…", // ← to be replaced
  "fileName": "string",
  "fileSizeBytes": 123 | null,
  "durationSeconds": 12.3 | null,
  "createdAt": "date-time"
}
```

Auth: all endpoints expect `Authorization: Bearer <supabase access token>`.
