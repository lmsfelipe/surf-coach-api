"""Surfboard CRUD: ownership boundaries, partial updates, validation."""

from uuid import UUID, uuid4

import pytest

from app.api import surfboards as surfboards_api
from app.main import app
from app.services.surfboards import SurfboardService
from tests.conftest import make_token
from tests.fake_deps import FakeSurfboardRepo

BASE = "/api/v1/surfboards/"


@pytest.fixture
def repo() -> FakeSurfboardRepo:
    return FakeSurfboardRepo()


@pytest.fixture(autouse=True)
def _override_surfboard_service(repo):
    app.dependency_overrides[surfboards_api.get_surfboard_service] = lambda: SurfboardService(repo)  # type: ignore[arg-type]
    yield
    app.dependency_overrides.pop(surfboards_api.get_surfboard_service, None)


async def _create(client, headers, **overrides) -> dict:
    payload = {"boardType": "shortboard", "boardSize": 5.9, "volume": 27.5, "label": "Daily"}
    payload.update(overrides)
    r = await client.post(BASE, headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_surfboard_returns_201_owned_by_caller(client, auth_headers, user_id):
    async with client as c:
        body = await _create(c, auth_headers)
    assert body["profileId"] == str(user_id)
    assert body["boardType"] == "shortboard"
    assert body["boardSize"] == 5.9
    assert body["volume"] == 27.5
    assert body["label"] == "Daily"


async def test_list_returns_only_the_callers_boards(client, auth_headers):
    other = {"Authorization": f"Bearer {make_token(uuid4())}"}
    async with client as c:
        await _create(c, auth_headers, label="Mine")
        await _create(c, other, label="Theirs")
        r = await c.get(BASE, headers=auth_headers)
    assert r.status_code == 200
    labels = [b["label"] for b in r.json()]
    assert labels == ["Mine"]


async def test_list_is_newest_first(client, auth_headers):
    async with client as c:
        await _create(c, auth_headers, label="older")
        await _create(c, auth_headers, label="newer")
        r = await c.get(BASE, headers=auth_headers)
    assert [b["label"] for b in r.json()] == ["newer", "older"]


async def test_get_surfboard_returns_the_board(client, auth_headers):
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.get(f"{BASE}{created['id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


async def test_get_other_users_board_returns_403(client, auth_headers):
    other = {"Authorization": f"Bearer {make_token(uuid4())}"}
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.get(f"{BASE}{created['id']}", headers=other)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "SURFBOARD_FORBIDDEN"


async def test_get_unknown_board_returns_404(client, auth_headers):
    async with client as c:
        r = await c.get(f"{BASE}{uuid4()}", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SURFBOARD_NOT_FOUND"


async def test_patch_updates_only_supplied_fields(client, auth_headers):
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.patch(f"{BASE}{created['id']}", headers=auth_headers, json={"label": "Renamed"})
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Renamed"
    # Untouched fields survive the partial update.
    assert body["boardType"] == created["boardType"]
    assert body["boardSize"] == created["boardSize"]
    assert body["volume"] == created["volume"]


async def test_patch_with_empty_body_is_a_no_op(client, auth_headers):
    """exclude_unset makes an empty payload short-circuit before the repo write."""
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.patch(f"{BASE}{created['id']}", headers=auth_headers, json={})
    assert r.status_code == 200
    assert r.json()["label"] == created["label"]


async def test_patch_can_clear_an_optional_field(client, auth_headers):
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.patch(f"{BASE}{created['id']}", headers=auth_headers, json={"label": None})
    assert r.status_code == 200
    assert r.json()["label"] is None


async def test_patch_other_users_board_returns_403(client, auth_headers):
    other = {"Authorization": f"Bearer {make_token(uuid4())}"}
    async with client as c:
        created = await _create(c, auth_headers)
        r = await c.patch(f"{BASE}{created['id']}", headers=other, json={"label": "hijack"})
    assert r.status_code == 403


async def test_delete_returns_204_and_removes_the_board(client, auth_headers):
    async with client as c:
        created = await _create(c, auth_headers)
        d = await c.delete(f"{BASE}{created['id']}", headers=auth_headers)
        assert d.status_code == 204
        g = await c.get(f"{BASE}{created['id']}", headers=auth_headers)
    assert g.status_code == 404


async def test_delete_other_users_board_returns_403_and_keeps_it(client, auth_headers, repo):
    other = {"Authorization": f"Bearer {make_token(uuid4())}"}
    async with client as c:
        created = await _create(c, auth_headers)
        d = await c.delete(f"{BASE}{created['id']}", headers=other)
    assert d.status_code == 403
    assert await repo.get_by_id(UUID(created["id"])) is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"boardType": "jetski", "boardSize": 5.9},  # not in the BoardType literal
        {"boardType": "shortboard", "boardSize": 0},  # gt=0
        {"boardType": "shortboard", "boardSize": -1},
        {"boardType": "shortboard", "boardSize": 5.9, "volume": 0},  # gt=0
        {"boardSize": 5.9},  # board_type is required
        {"boardType": "shortboard"},  # board_size is required
    ],
)
async def test_create_validation_errors_return_400(client, auth_headers, payload):
    async with client as c:
        r = await c.post(BASE, headers=auth_headers, json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_label_over_max_length_rejected(client, auth_headers):
    async with client as c:
        r = await c.post(
            BASE,
            headers=auth_headers,
            json={"boardType": "shortboard", "boardSize": 5.9, "label": "x" * 201},
        )
    assert r.status_code == 400


async def test_surfboard_endpoints_require_auth(client):
    async with client as c:
        assert (await c.get(BASE)).status_code == 401
        assert (await c.post(BASE, json={})).status_code == 401
        assert (await c.get(f"{BASE}{uuid4()}")).status_code == 401
        assert (await c.patch(f"{BASE}{uuid4()}", json={})).status_code == 401
        assert (await c.delete(f"{BASE}{uuid4()}")).status_code == 401
