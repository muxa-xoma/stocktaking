"""Functional scenario: user signs up and logs in over the REST API.

Moved as-is from ``tests/api/test_api_rest.py`` (full user path from input
through the real service stack to the final result — a working token).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.functional._functional_utils import PASSWORD, USERNAME

if TYPE_CHECKING:
    import httpx


async def test_register_and_login_happy_path(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["username"] == USERNAME
    assert isinstance(body["id"], int)

    response = await client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert isinstance(response.json()["token"], str)
    assert response.json()["token"]
