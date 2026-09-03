"""StorageClient: Supabase failures must surface as the app's own error types,
never as raw supabase/httpx exceptions leaking through the service layer.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import (
    RangeNotSatisfiableError,
    StorageDownloadError,
    StorageUploadFailedError,
)
from app.core.storage import StorageClient

BUCKET = "surf-media"
KEY = "profile/session/media.jpg"


class _StubBucket:
    def __init__(self, *, fail: Exception | None = None, public_url: str = "https://cdn/x.jpg"):
        self._fail = fail
        self._public_url = public_url
        self.uploads: list[dict] = []
        self.removed: list[list[str]] = []
        self.downloaded: list[str] = []

    def upload(self, *, path, file, file_options):
        if self._fail:
            raise self._fail
        # storage3 consumes a reader for the file-backed variant.
        data = file.read() if hasattr(file, "read") else file
        self.uploads.append({"path": path, "data": data, "options": file_options})

    def get_public_url(self, key):
        return f"{self._public_url}?key={key}"

    def download(self, key):
        self.downloaded.append(key)
        if self._fail:
            raise self._fail
        return b"object-bytes"

    def remove(self, keys):
        if self._fail:
            raise self._fail
        self.removed.append(keys)


class _StubSupabase:
    def __init__(self, bucket: _StubBucket) -> None:
        self.storage = self
        self._bucket = bucket

    def from_(self, name):
        return self._bucket


def _client(bucket: _StubBucket) -> StorageClient:
    return StorageClient(client=_StubSupabase(bucket), bucket=BUCKET)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# upload / upload_file
# ---------------------------------------------------------------------------


def test_upload_sends_the_content_type_and_returns_the_public_url():
    bucket = _StubBucket()
    url = _client(bucket).upload(KEY, b"bytes", "image/jpeg")

    assert bucket.uploads[0]["data"] == b"bytes"
    assert bucket.uploads[0]["options"]["content-type"] == "image/jpeg"
    assert KEY in url


def test_upload_upserts_so_an_optimized_video_can_replace_the_raw_one():
    bucket = _StubBucket()
    _client(bucket).upload(KEY, b"bytes", "video/mp4")
    assert bucket.uploads[0]["options"]["upsert"] == "true"


def test_upload_failure_becomes_storage_upload_failed():
    with pytest.raises(StorageUploadFailedError):
        _client(_StubBucket(fail=RuntimeError("supabase down"))).upload(KEY, b"b", "image/jpeg")


def test_upload_file_streams_from_disk(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video-bytes")
    bucket = _StubBucket()

    _client(bucket).upload_file(KEY, str(src), "video/mp4")

    assert bucket.uploads[0]["data"] == b"video-bytes"


def test_upload_file_failure_becomes_storage_upload_failed(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video-bytes")
    with pytest.raises(StorageUploadFailedError):
        _client(_StubBucket(fail=RuntimeError("nope"))).upload_file(KEY, str(src), "video/mp4")


def test_upload_file_missing_source_becomes_storage_upload_failed(tmp_path):
    with pytest.raises(StorageUploadFailedError):
        _client(_StubBucket()).upload_file(KEY, str(tmp_path / "absent.mp4"), "video/mp4")


# ---------------------------------------------------------------------------
# download / delete
# ---------------------------------------------------------------------------


def test_download_returns_the_object_bytes():
    assert _client(_StubBucket()).download(KEY) == b"object-bytes"


def test_download_failure_becomes_storage_download_error():
    with pytest.raises(StorageDownloadError):
        _client(_StubBucket(fail=RuntimeError("gone"))).download(KEY)


def test_delete_removes_the_key():
    bucket = _StubBucket()
    _client(bucket).delete(KEY)
    assert bucket.removed == [[KEY]]


def test_delete_swallows_its_errors():
    """Cleanup runs on the failure path; raising here would mask the real error."""
    _client(_StubBucket(fail=RuntimeError("already gone"))).delete(KEY)


# ---------------------------------------------------------------------------
# download_range (buffered variant of stream_range)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http(monkeypatch):
    """Route the client's outbound httpx calls to ``handler`` (see test_storage_stream)."""

    def _install(handler):
        real_cls = httpx.AsyncClient

        class _MockedClient(real_cls):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("app.core.storage.httpx.AsyncClient", _MockedClient)

    return _install


async def test_download_range_returns_the_whole_object(mock_http):
    mock_http(
        lambda req: httpx.Response(
            200, content=b"full-object", headers={"content-type": "video/mp4"}
        )
    )
    result = await _client(_StubBucket()).download_range(KEY)

    assert result.status_code == 200
    assert result.content == b"full-object"
    assert result.content_type == "video/mp4"
    assert result.content_length == len(b"full-object")


async def test_download_range_forwards_the_range_header(mock_http):
    seen: dict = {}

    def _handler(request):
        seen["range"] = request.headers.get("range")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            206,
            content=b"partial",
            headers={"content-range": "bytes 0-6/100", "content-type": "video/mp4"},
        )

    mock_http(_handler)
    result = await _client(_StubBucket()).download_range(KEY, "bytes=0-6")

    assert seen["range"] == "bytes=0-6"
    assert seen["auth"].startswith("Bearer ")
    assert result.status_code == 206
    assert result.content_range == "bytes 0-6/100"


async def test_download_range_416_becomes_range_not_satisfiable(mock_http):
    mock_http(lambda req: httpx.Response(416, content=b""))
    with pytest.raises(RangeNotSatisfiableError):
        await _client(_StubBucket()).download_range(KEY, "bytes=999999-")


@pytest.mark.parametrize("status", [400, 401, 404, 500])
async def test_download_range_other_errors_become_storage_download_error(mock_http, status):
    mock_http(lambda req: httpx.Response(status, content=b"nope"))
    with pytest.raises(StorageDownloadError):
        await _client(_StubBucket()).download_range(KEY)


async def test_download_range_transport_failure_becomes_storage_download_error(mock_http):
    def _boom(request):
        raise httpx.ConnectError("connection refused")

    mock_http(_boom)
    with pytest.raises(StorageDownloadError):
        await _client(_StubBucket()).download_range(KEY)


async def test_download_range_defaults_the_content_type_when_absent(mock_http):
    mock_http(lambda req: httpx.Response(200, content=b"bytes"))
    result = await _client(_StubBucket()).download_range(KEY)
    assert result.content_type == "application/octet-stream"
