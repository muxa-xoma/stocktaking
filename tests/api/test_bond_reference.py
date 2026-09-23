"""REST tests for the MOEX bond-reference endpoints (SC-3/SC-4).

``GET /api/bonds/reference/search`` and ``GET /api/bonds/reference/{isin}``
backed by the in-memory ISS responder from ``tests/conftest.py``
(``moex_mock_handler``): success shapes, validation, bearer-auth protection
and the 404/503 error mapping installed by ``register_exception_handlers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from tests.rest_utils import _register_and_login

if TYPE_CHECKING:
    from bond_accounting.market_data import BondReferenceService
    from tests.conftest import _MoexMock


#: One known MOEX bond: search-level fields (lowercase) + detail fields
#: (UPPERCASE), see ``_MoexMock`` in ``tests/conftest.py``.
_MOEX_BOND = {
    "secid": "SU26207RMFS4",
    "isin": "RU000A0JX0J2",
    "shortname": "ОФЗ 26207",
    "name": "ОФЗ 26207 26234",
    "group": "stock_bonds",
    "type": "bond",
    "emitent_title": "Минфин",
    "SECID": "SU26207RMFS4",
    "ISIN": "RU000A0JX0J2",
    "SHORTNAME": "ОФЗ 26207",
    "SECNAME": "ОФЗ 26207 26234",
    "FACEVALUE": 1000,
    "COUPONPERCENT": 8.15,
    "COUPONPERIOD": 182,
    "MATDATE": "2041-02-26",
    "ISSUER": "Минфин",
}

#: The same bond as serialized by ``BondReferenceResponse``.
_EXPECTED_REFERENCE = {
    "isin": "RU000A0JX0J2",
    "name": "ОФЗ 26207",
    "nominal": 1000,
    "coupon_rate": 8.15,
    "coupon_period_days": 182,
    "maturity_date": "2041-02-26",
    "issuer": "Минфин",
}


async def test_search_returns_results(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock
) -> None:
    """A matching query returns the mapped references (200)."""
    moex_mock_handler.bonds = [_MOEX_BOND]
    headers = await _register_and_login(client)
    response = await client.get(
        "/api/bonds/reference/search", params={"q": "26207"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"results": [_EXPECTED_REFERENCE]}


async def test_search_no_results_is_ok(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock
) -> None:
    """An empty registry is a regular 200 with an empty result list."""
    assert moex_mock_handler.bonds == []
    headers = await _register_and_login(client)
    response = await client.get(
        "/api/bonds/reference/search", params={"q": "26207"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"results": []}


@pytest.mark.parametrize("limit", [1, 50])
async def test_search_limit_bounds_accepted(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock, limit: int
) -> None:
    """The limit parameter accepts its full documented range 1..50."""
    moex_mock_handler.bonds = [_MOEX_BOND]
    headers = await _register_and_login(client)
    response = await client.get(
        "/api/bonds/reference/search", params={"q": "26207", "limit": limit}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {},  # q is required
        {"q": ""},
        {"q": "a" * 101},
        {"q": "26207", "limit": 0},
        {"q": "26207", "limit": 51},
    ],
)
async def test_search_invalid_params_unprocessable(client: httpx.AsyncClient, params: dict) -> None:
    """Out-of-range/missing q and limit are rejected with 422."""
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds/reference/search", params=params, headers=headers)
    assert response.status_code == 422, response.text


async def test_search_without_token_unauthorized(client: httpx.AsyncClient) -> None:
    """The search endpoint requires a bearer token (q supplied, so 422
    validation of the missing parameter does not preempt the auth check)."""
    response = await client.get("/api/bonds/reference/search", params={"q": "26207"})
    assert response.status_code == 401


async def test_get_by_isin_returns_reference(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock
) -> None:
    """A known ISIN resolves to its reference (200)."""
    moex_mock_handler.bonds = [_MOEX_BOND]
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds/reference/RU000A0JX0J2", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == _EXPECTED_REFERENCE


async def test_get_by_isin_unknown_isin_not_found(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock
) -> None:
    """An ISIN absent from the registry maps NoBondsFoundError to 404."""
    moex_mock_handler.bonds = [_MOEX_BOND]
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds/reference/XX0000000001", headers=headers)
    assert response.status_code == 404, response.text
    assert "detail" in response.json()


async def test_get_by_isin_without_token_unauthorized(client: httpx.AsyncClient) -> None:
    """The ISIN endpoint requires a bearer token."""
    response = await client.get("/api/bonds/reference/RU000A0JX0J2")
    assert response.status_code == 401


@pytest.mark.parametrize(
    "error",
    [
        # Transport failure -> ProviderUnavailableError -> 503.
        "unavailable",
        # Request timeout -> ProviderTimeoutError -> 503.
        "timeout",
    ],
)
async def test_reference_provider_outage_service_unavailable(
    client: httpx.AsyncClient, moex_mock_handler: _MoexMock, error: str
) -> None:
    """A MOEX outage surfaces as 503 on both reference endpoints."""
    moex_mock_handler.error = (
        httpx.ConnectError("moex down")
        if error == "unavailable"
        else httpx.ReadTimeout("timed out")
    )
    headers = await _register_and_login(client)

    response = await client.get(
        "/api/bonds/reference/search", params={"q": "26207"}, headers=headers
    )
    assert response.status_code == 503, response.text

    response = await client.get("/api/bonds/reference/RU000A0JX0J2", headers=headers)
    assert response.status_code == 503, response.text


async def test_reference_disabled_service_unavailable(
    client: httpx.AsyncClient,
    bond_reference_service: BondReferenceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled integration (``market_data.enabled = false``) answers 503."""
    monkeypatch.setattr(bond_reference_service, "enabled", False)
    headers = await _register_and_login(client)

    response = await client.get(
        "/api/bonds/reference/search", params={"q": "26207"}, headers=headers
    )
    assert response.status_code == 503, response.text

    response = await client.get("/api/bonds/reference/RU000A0JX0J2", headers=headers)
    assert response.status_code == 503, response.text
