"""Tests for StorageClient.stream_range — the real httpx path, against a mock transport."""

import httpx
import pytest

from app.core.errors import RangeNotSatisfiableError, StorageDownloadError
from app.core.storage import StorageClient, StorageStream

OBJECT = b"0123456789" * 200  # 2000 bytes


@pytest.fixture
def open_stream(monkeypatch):
    """Open a stream with the client's outbound requests routed to ``handler``."""

    async def _open(handler, range_header: str | None = None) -> StorageStream:
        real_cls = httpx.AsyncClient

        class _MockedClient(real_cls):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("app.core.storage.httpx.AsyncClient", _MockedClient)
        client = StorageClient(client=None, bucket="surf-media")  # type: ignore[arg-type]
        return await client.stream_range("user/session/media.mp4", range_header)

    return _open


async def _drain(stream: StorageStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream.aiter_bytes()])
    finally:
        await stream.aclose()


async def test_stream_range_relays_full_object(open_stream):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=OBJECT, headers={"content-type": "video/mp4"})

    stream = await open_stream(handler)
    body = await _drain(stream)

    assert stream.status_code == 200
    assert stream.content_type == "video/mp4"
    assert stream.content_length == len(OBJECT)
    assert stream.content_range is None
    assert body == OBJECT
    assert "range" not in requests[0].headers


async def test_stream_range_preserves_206_headers(open_stream):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=10-19"
        return httpx.Response(
            206,
            content=OBJECT[10:20],
            headers={
                "content-type": "video/mp4",
                "content-range": f"bytes 10-19/{len(OBJECT)}",
            },
        )

    stream = await open_stream(handler, "bytes=10-19")
    body = await _drain(stream)

    assert stream.status_code == 206
    assert stream.content_range == f"bytes 10-19/{len(OBJECT)}"
    assert stream.content_length == 10
    assert body == OBJECT[10:20]


async def test_stream_range_416_raises_range_not_satisfiable(open_stream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(416, content=b"")

    with pytest.raises(RangeNotSatisfiableError):
        await open_stream(handler, "bytes=99999-")


async def test_stream_range_upstream_error_raises_storage_error(open_stream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(StorageDownloadError):
        await open_stream(handler)


async def test_stream_range_transport_failure_raises_storage_error(open_stream):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(StorageDownloadError):
        await open_stream(handler)
