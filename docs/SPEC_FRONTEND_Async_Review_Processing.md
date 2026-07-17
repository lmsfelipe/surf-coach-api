# SPEC — Async Review & Training Plan Processing: Frontend Implementation

> **Feature:** Reviews and training plans now generate in the background
> **Last updated:** 2026-07-14
> **Companion doc:** `SPEC_BACKEND_Async_Review_Processing.md`
> **Stack:** React + TypeScript + TanStack Query + shadcn/ui

---

## 1. What Changed on the Backend

Review and training plan generation no longer happen synchronously. Both
creation endpoints now return immediately with `202 Accepted` and a partial
object. The frontend is responsible for polling until generation completes.

### 1.1 Review creation

**Before:**
```
POST /api/v1/reviews/
→ 201 { id, narrative, improvementTips, scores, … }  (after 10–60 s)
```

**After:**
```
POST /api/v1/reviews/
→ 202 { id, status: "processing", narrative: null, improvementTips: null, … }
```

Poll `GET /api/v1/reviews/{id}` until `status` is `"completed"` or `"failed"`.

### 1.2 Training plan creation

```
POST /api/v1/ai/training-plans/
→ 202 { id, status: "processing", workouts: [], … }
```

Poll `GET /api/v1/ai/training-plans/{id}` until done.

### 1.3 New `status` field

Both `ReviewOut` and `TrainingPlanResponse` now always include:

| Field | Type | Notes |
|---|---|---|
| `status` | `"processing" \| "completed" \| "failed"` | Always present |
| `errorMessage` | `string \| null` | Populated only when `status === "failed"` |

When `status === "processing"`, content fields (`narrative`, `improvementTips`,
`scores`, `workouts`, etc.) are `null` or `[]`.

### 1.4 New endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/reviews/{id}/retry` | Re-enqueue a failed review → `202` |
| `POST` | `/api/v1/ai/training-plans/{id}/retry` | Re-enqueue a failed plan → `202` |

Both retry endpoints return `409 REVIEW_NOT_RETRYABLE` / `TRAINING_PLAN_NOT_RETRYABLE`
if the resource is not in `"failed"` state.

---

## 2. Updated TypeScript Types

```ts
// src/types/review.ts

export type ReviewStatus = "processing" | "completed" | "failed";

export interface Review {
  id: string;
  sessionId: string;
  profileId: string;
  status: ReviewStatus;
  errorMessage: string | null;
  narrative: string | null;
  improvementTips: string[] | null;
  scoreFlow: number | null;
  scoreDrop: number | null;
  scoreBalance: number | null;
  scoreWaveSelection: number | null;
  scoreManeuvers: number | null;
  scoreArms: number | null;
  overallScore: number | null;
  aiModelVersion: string | null;
  createdAt: string;
}
```

```ts
// src/types/training.ts

export type PlanStatus = "processing" | "completed" | "failed";

export interface Exercise {
  id: string;
  sequenceNumber: number;
  name: string;
  description: string;
  sets: number;
  reps: string;
  videoUrl: string | null;
  createdAt: string;
}

export interface Workout {
  id: string;
  planId: string;
  sequenceNumber: number;
  title: string;
  focusArea: string;
  createdAt: string;
  exercises: Exercise[];
}

export interface TrainingPlan {
  id: string;
  reviewId: string;
  profileId: string;
  status: PlanStatus;
  errorMessage: string | null;
  generatedBy: string;
  aiModelVersion: string | null;
  createdAt: string;
  workouts: Workout[];
}
```

---

## 3. Polling Strategy

Per spec, the recommended polling schedule is:

| Time since creation | Interval |
|---|---|
| 0 – 30 s | Every 3 s |
| 30 s – 5 min | Every 10 s |
| After 5 min | Stop polling, surface a timeout message |

TanStack Query's `refetchInterval` supports a function form that receives the
latest data, making this straightforward to implement:

```ts
// src/hooks/useReview.ts

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { Review } from "@/types/review";

function getPollingInterval(
  review: Review | undefined,
  startedAt: number,
): number | false {
  if (!review || review.status !== "processing") return false;

  const elapsed = Date.now() - startedAt;
  if (elapsed > 5 * 60 * 1000) return false;   // 5 min — stop polling
  if (elapsed < 30 * 1000) return 3_000;        // first 30 s — every 3 s
  return 10_000;                                 // after 30 s — every 10 s
}

export function useReview(reviewId: string | undefined) {
  const startedAt = Date.now();

  return useQuery<Review>({
    queryKey: ["review", reviewId],
    queryFn: () => apiClient.get(`/api/v1/reviews/${reviewId}`),
    enabled: !!reviewId,
    refetchInterval: (query) =>
      getPollingInterval(query.state.data, startedAt),
  });
}
```

> **Important:** `startedAt` must be captured **once** at hook instantiation,
> not on every render. The closure above works correctly because `startedAt`
> is captured at the time `useReview` is first called for a given `reviewId`.

Equivalent hook for training plans:

```ts
// src/hooks/useTrainingPlan.ts

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { TrainingPlan } from "@/types/training";

function getPollingInterval(
  plan: TrainingPlan | undefined,
  startedAt: number,
): number | false {
  if (!plan || plan.status !== "processing") return false;
  const elapsed = Date.now() - startedAt;
  if (elapsed > 5 * 60 * 1000) return false;
  if (elapsed < 30 * 1000) return 3_000;
  return 10_000;
}

export function useTrainingPlan(planId: string | undefined) {
  const startedAt = Date.now();

  return useQuery<TrainingPlan>({
    queryKey: ["training-plan", planId],
    queryFn: () => apiClient.get(`/api/v1/ai/training-plans/${planId}`),
    enabled: !!planId,
    refetchInterval: (query) =>
      getPollingInterval(query.state.data, startedAt),
  });
}
```

---

## 4. Mutations

### 4.1 Create review

```ts
// src/hooks/useCreateReview.ts

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { Review } from "@/types/review";

export function useCreateReview() {
  const queryClient = useQueryClient();

  return useMutation<Review, ApiError, { sessionId: string }>({
    mutationFn: ({ sessionId }) =>
      apiClient.post("/api/v1/reviews/", { sessionId }),
    onSuccess: (review) => {
      // Seed the cache immediately so useReview starts from the right state
      queryClient.setQueryData(["review", review.id], review);
      queryClient.invalidateQueries({ queryKey: ["session", review.sessionId] });
    },
  });
}
```

### 4.2 Retry review

```ts
// src/hooks/useRetryReview.ts

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { Review } from "@/types/review";

export function useRetryReview() {
  const queryClient = useQueryClient();

  return useMutation<Review, ApiError, { reviewId: string }>({
    mutationFn: ({ reviewId }) =>
      apiClient.post(`/api/v1/reviews/${reviewId}/retry`, {}),
    onSuccess: (review) => {
      // Update cache with processing state — polling resumes automatically
      queryClient.setQueryData(["review", review.id], review);
    },
  });
}
```

### 4.3 Create training plan

```ts
// src/hooks/useCreateTrainingPlan.ts

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { TrainingPlan } from "@/types/training";

export function useCreateTrainingPlan() {
  const queryClient = useQueryClient();

  return useMutation<TrainingPlan, ApiError, { reviewId: string }>({
    mutationFn: ({ reviewId }) =>
      apiClient.post("/api/v1/ai/training-plans/", { reviewId }),
    onSuccess: (plan) => {
      queryClient.setQueryData(["training-plan", plan.id], plan);
      queryClient.invalidateQueries({ queryKey: ["training-plans"] });
    },
  });
}
```

### 4.4 Retry training plan

```ts
// src/hooks/useRetryTrainingPlan.ts

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/apiClient";
import type { TrainingPlan } from "@/types/training";

export function useRetryTrainingPlan() {
  const queryClient = useQueryClient();

  return useMutation<TrainingPlan, ApiError, { planId: string }>({
    mutationFn: ({ planId }) =>
      apiClient.post(`/api/v1/ai/training-plans/${planId}/retry`, {}),
    onSuccess: (plan) => {
      queryClient.setQueryData(["training-plan", plan.id], plan);
    },
  });
}
```

---

## 5. UI States

Every screen that displays a review or training plan must handle three states.
Use the pattern below as a reference — adapt to your actual component structure.

### 5.1 Review screen

```tsx
// src/pages/ReviewPage.tsx (simplified)

import { useReview } from "@/hooks/useReview";
import { useRetryReview } from "@/hooks/useRetryReview";
import { ReviewSkeleton } from "@/components/ReviewSkeleton";
import { ReviewError } from "@/components/ReviewError";
import { ReviewContent } from "@/components/ReviewContent";

interface ReviewPageProps {
  reviewId: string;
}

export function ReviewPage({ reviewId }: ReviewPageProps) {
  const { data: review, isLoading } = useReview(reviewId);
  const { mutate: retry, isPending: isRetrying } = useRetryReview();

  if (isLoading || !review) return <ReviewSkeleton />;

  if (review.status === "processing") {
    return <ReviewSkeleton message="Analisando sua sessão…" />;
  }

  if (review.status === "failed") {
    return (
      <ReviewError
        message={review.errorMessage ?? "Não foi possível gerar a análise."}
        onRetry={() => retry({ reviewId: review.id })}
        isRetrying={isRetrying}
      />
    );
  }

  // status === "completed"
  return <ReviewContent review={review} />;
}
```

### 5.2 ReviewSkeleton — processing state

```tsx
// src/components/ReviewSkeleton.tsx

import { Skeleton } from "@/components/ui/skeleton";

interface ReviewSkeletonProps {
  message?: string;
}

export function ReviewSkeleton({
  message = "Carregando análise…",
}: ReviewSkeletonProps) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground animate-pulse">{message}</p>
      <Skeleton className="h-6 w-1/3" />
      <Skeleton className="h-24 w-full" />
      <div className="grid grid-cols-3 gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
      <Skeleton className="h-20 w-full" />
    </div>
  );
}
```

### 5.3 ReviewError — failed state

```tsx
// src/components/ReviewError.tsx

import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

interface ReviewErrorProps {
  message: string;
  onRetry: () => void;
  isRetrying: boolean;
}

export function ReviewError({ message, onRetry, isRetrying }: ReviewErrorProps) {
  return (
    <div className="space-y-4">
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertTitle>Análise não concluída</AlertTitle>
        <AlertDescription>{message}</AlertDescription>
      </Alert>
      <Button onClick={onRetry} disabled={isRetrying} variant="outline">
        {isRetrying ? "Reagendando…" : "Tentar novamente"}
      </Button>
    </div>
  );
}
```

### 5.4 Training plan screen

Same three-state pattern applies:

```tsx
// src/pages/TrainingPlanPage.tsx (simplified)

import { useTrainingPlan } from "@/hooks/useTrainingPlan";
import { useRetryTrainingPlan } from "@/hooks/useRetryTrainingPlan";
import { TrainingPlanSkeleton } from "@/components/TrainingPlanSkeleton";
import { TrainingPlanError } from "@/components/TrainingPlanError";
import { TrainingPlanContent } from "@/components/TrainingPlanContent";

interface TrainingPlanPageProps {
  planId: string;
}

export function TrainingPlanPage({ planId }: TrainingPlanPageProps) {
  const { data: plan, isLoading } = useTrainingPlan(planId);
  const { mutate: retry, isPending: isRetrying } = useRetryTrainingPlan();

  if (isLoading || !plan) return <TrainingPlanSkeleton />;

  if (plan.status === "processing") {
    return <TrainingPlanSkeleton message="Criando seu plano de treino…" />;
  }

  if (plan.status === "failed") {
    return (
      <TrainingPlanError
        message={plan.errorMessage ?? "Não foi possível gerar o plano."}
        onRetry={() => retry({ planId: plan.id })}
        isRetrying={isRetrying}
      />
    );
  }

  return <TrainingPlanContent plan={plan} />;
}
```

---

## 6. Post-creation Navigation Flow

After `POST /api/v1/reviews/` returns `202`, navigate to the review page
**immediately** — the `status: "processing"` state is the loading indicator.
Do **not** wait for the mutation to settle before navigating.

```tsx
// Example: session detail page — "Generate Review" button

const { mutate: createReview, isPending } = useCreateReview();

function handleGenerateReview() {
  createReview(
    { sessionId },
    {
      onSuccess: (review) => {
        navigate(`/reviews/${review.id}`);
        // ReviewPage polls automatically from this point
      },
    },
  );
}
```

The same applies to training plan creation — navigate to the plan page as soon
as the `202` response arrives.

---

## 7. 5-Minute Timeout Handling

When the polling window expires (5 min with no completion), polling stops and
the review remains in `"processing"` state visually. This should not happen in
production, but the UI must handle it gracefully.

Detect this by tracking whether polling is still active:

```ts
// src/hooks/useReview.ts (extended)

export function useReview(reviewId: string | undefined) {
  const startedAt = useRef(Date.now());

  const query = useQuery<Review>({
    queryKey: ["review", reviewId],
    queryFn: () => apiClient.get(`/api/v1/reviews/${reviewId}`),
    enabled: !!reviewId,
    refetchInterval: (q) =>
      getPollingInterval(q.state.data, startedAt.current),
  });

  const timedOut =
    query.data?.status === "processing" &&
    Date.now() - startedAt.current > 5 * 60 * 1000 &&
    !query.isFetching;

  return { ...query, timedOut };
}
```

> Use `useRef` (not a plain `Date.now()` call) to avoid re-creating `startedAt`
> across re-renders.

When `timedOut` is `true`, show a soft warning:

```tsx
{timedOut && (
  <Alert>
    <AlertDescription>
      A análise está demorando mais que o esperado. Tente recarregar a página
      em alguns instantes.
    </AlertDescription>
  </Alert>
)}
```

---

## 8. Error Code Reference

Add these codes to your central error map:

```ts
// src/lib/errors.ts

export const REVIEW_ERRORS: Record<string, string> = {
  REVIEW_ALREADY_EXISTS:     "Já existe uma análise para esta sessão.",
  REVIEW_NOT_FOUND:          "Análise não encontrada.",
  REVIEW_NOT_RETRYABLE:      "Somente análises com falha podem ser reagendadas.",
  NO_MEDIA_FOR_SESSION:      "Nenhuma mídia encontrada. Envie fotos ou vídeos antes de gerar a análise.",
  AI_GENERATION_FAILED:      "O serviço de IA está temporariamente indisponível.",
  AI_PARSE_FAILED:           "Resposta inesperada do serviço de IA.",
};

export const TRAINING_PLAN_ERRORS: Record<string, string> = {
  TRAINING_PLAN_ALREADY_EXISTS:  "Já existe um plano de treino para esta análise.",
  TRAINING_PLAN_NOT_RETRYABLE:   "Somente planos com falha podem ser reagendados.",
  REVIEW_NOT_FOUND:              "Análise não encontrada. Gere uma análise antes de criar um plano.",
  AI_GENERATION_FAILED:          "O serviço de IA está temporariamente indisponível.",
};
```

The `errorMessage` field on failed resources is a pre-translated user-friendly
string coming directly from the backend. Prefer using it directly over your
local map, but fall back to the map when `errorMessage` is null.

```ts
function getDisplayMessage(
  errorMessage: string | null,
  fallbackCode: string,
  map: Record<string, string>,
): string {
  return errorMessage ?? map[fallbackCode] ?? "Erro desconhecido.";
}
```

---

## 9. Handling `409 REVIEW_NOT_RETRYABLE`

The retry endpoints return `409` if the review/plan is not in `"failed"` state.
This should not happen if the UI conditionally renders the retry button only
when `status === "failed"`, but guard for it anyway:

```ts
onError: (error: ApiError) => {
  if (error.code === "REVIEW_NOT_RETRYABLE") {
    // Refetch to sync state — user may have already retried in another tab
    queryClient.invalidateQueries({ queryKey: ["review", reviewId] });
    return;
  }
  // surface generic error toast
},
```

---

## 10. MSW Mock Handlers

```ts
// src/mocks/handlers/reviews.ts

import { http, HttpResponse } from "msw";
import type { Review } from "@/types/review";

const processingReview: Review = {
  id: "review-123",
  sessionId: "session-abc",
  profileId: "profile-xyz",
  status: "processing",
  errorMessage: null,
  narrative: null,
  improvementTips: null,
  scoreFlow: null,
  scoreDrop: null,
  scoreBalance: null,
  scoreWaveSelection: null,
  scoreManeuvers: null,
  scoreArms: null,
  overallScore: null,
  aiModelVersion: null,
  createdAt: new Date().toISOString(),
};

const completedReview: Review = {
  ...processingReview,
  status: "completed",
  narrative: "Boa sessão! Seu equilíbrio melhorou significativamente.",
  improvementTips: [
    "Trabalhe a posição dos braços na remada.",
    "Pratique a virada no pico.",
    "Melhore a leitura das ondas.",
  ],
  scoreFlow: 7.5,
  scoreDrop: 8.0,
  scoreBalance: 7.0,
  scoreWaveSelection: 6.5,
  scoreManeuvers: 7.0,
  scoreArms: 6.0,
  overallScore: 7.0,
  aiModelVersion: "gemini-2.0-flash",
};

const failedReview: Review = {
  ...processingReview,
  status: "failed",
  errorMessage: "O serviço de IA está temporariamente indisponível.",
};

export const reviewHandlers = [
  // Create review → 202 processing
  http.post("/api/v1/reviews/", () =>
    HttpResponse.json(processingReview, { status: 202 }),
  ),

  // Poll → returns completed after first poll (adjust for test needs)
  http.get("/api/v1/reviews/:reviewId", () =>
    HttpResponse.json(completedReview),
  ),

  // Retry failed review → 202 processing
  http.post("/api/v1/reviews/:reviewId/retry", () =>
    HttpResponse.json({ ...processingReview, status: "processing" }, { status: 202 }),
  ),

  // Retry non-failed review → 409
  http.post("/api/v1/reviews/:reviewId/retry", () =>
    HttpResponse.json(
      { error: { code: "REVIEW_NOT_RETRYABLE", message: "Only failed reviews can be retried." } },
      { status: 409 },
    ),
  ),
];
```

```ts
// src/mocks/handlers/trainingPlans.ts

import { http, HttpResponse } from "msw";
import type { TrainingPlan } from "@/types/training";

const processingPlan: TrainingPlan = {
  id: "plan-123",
  reviewId: "review-123",
  profileId: "profile-xyz",
  status: "processing",
  errorMessage: null,
  generatedBy: "ai",
  aiModelVersion: null,
  createdAt: new Date().toISOString(),
  workouts: [],
};

export const trainingPlanHandlers = [
  http.post("/api/v1/ai/training-plans/", () =>
    HttpResponse.json(processingPlan, { status: 202 }),
  ),

  http.get("/api/v1/ai/training-plans/:planId", () =>
    HttpResponse.json({ ...processingPlan, status: "completed", workouts: [] }),
  ),

  http.post("/api/v1/ai/training-plans/:planId/retry", () =>
    HttpResponse.json({ ...processingPlan, status: "processing" }, { status: 202 }),
  ),
];
```

---

## 11. Checklist

### Types & API layer
- [ ] `Review` type updated with `status`, `errorMessage`, and nullable content fields
- [ ] `TrainingPlan` type updated with `status`, `errorMessage`, and `workouts: []` default
- [ ] `apiClient` correctly handles `202 Accepted` (not just `2xx` where `200` was expected)

### Hooks
- [ ] `useReview` polls every 3 s for first 30 s, every 10 s after, stops at 5 min
- [ ] `useTrainingPlan` follows the same polling schedule
- [ ] `useCreateReview` seeds the query cache on success (`setQueryData`)
- [ ] `useCreateTrainingPlan` seeds the query cache on success
- [ ] `useRetryReview` updates cache with the returned `processing` state
- [ ] `useRetryTrainingPlan` updates cache with the returned `processing` state
- [ ] `startedAt` is captured with `useRef` (not re-created on re-render)

### UI — Review
- [ ] `status === "processing"` shows skeleton + "Analisando sua sessão…" message
- [ ] `status === "completed"` shows full review content (scores, narrative, tips)
- [ ] `status === "failed"` shows destructive alert with `errorMessage` and retry button
- [ ] Retry button calls `POST /reviews/{id}/retry` and polling resumes automatically
- [ ] 5-minute timeout shows soft warning without blocking the page
- [ ] Navigation happens immediately after `202` (no waiting for completion)

### UI — Training plan
- [ ] `status === "processing"` shows skeleton + "Criando seu plano de treino…" message
- [ ] `status === "completed"` shows full workout list
- [ ] `status === "failed"` shows alert with `errorMessage` and retry button
- [ ] Retry button is only rendered when `status === "failed"`

### Error handling
- [ ] `REVIEW_NOT_RETRYABLE` (409) on retry invalidates query to resync state
- [ ] `TRAINING_PLAN_NOT_RETRYABLE` (409) on retry invalidates query to resync state
- [ ] Error messages from `review.errorMessage` / `plan.errorMessage` are used directly (with fallback to local map)

### Tests / MSW
- [ ] MSW handler for `POST /reviews/` returns `202` with `status: "processing"`
- [ ] MSW handler for `GET /reviews/:id` returns `status: "completed"` on poll
- [ ] MSW handler for `POST /reviews/:id/retry` returns `202` with `status: "processing"`
- [ ] MSW handler for failed review scenario (for retry UI test)
- [ ] Component test: skeleton renders when `status === "processing"`
- [ ] Component test: retry button appears and calls mutation when `status === "failed"`
- [ ] Component test: retry button is absent when `status === "completed"` or `"processing"`
- [ ] Component test: polling stops when status is no longer `"processing"`
