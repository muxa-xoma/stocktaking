"""Wave 15 regression tests for cookie TTL + Secure attributes (Task 35).

Split out of the former monolithic ``test_wave15.py``: ``set_token_cookie``
/ ``clear_token_cookie`` JS commands over HTTP vs HTTPS, using a fake NiceGUI
client (no full user simulation is needed — the tests capture the emitted
``document.cookie`` JavaScript).

The ``jwt_service`` fixture comes from ``tests/conftest.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from nicegui.context import Context

from bond_accounting.ui import common as ui_common
from bond_accounting.ui.common import clear_token_cookie, set_token_cookie, token_max_age

if TYPE_CHECKING:
    from bond_accounting.auth import JwtService


# --------------------------------------------------------------------------- #
# fakes and capture helpers
# --------------------------------------------------------------------------- #


class _FakeUrl:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class _FakeRequest:
    def __init__(self, scheme: str) -> None:
        self.url = _FakeUrl(scheme)


class _FakeClient:
    def __init__(self, scheme: str) -> None:
        self.request = _FakeRequest(scheme)


@pytest.fixture
def captured_cookie_js(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every ``document.cookie`` JS command issued via ``run_javascript``.

    The module object is patched directly (not via a dotted import string):
    NiceGUI's test cleanup pops ``bond_accounting*`` from ``sys.modules`` after
    each UI test, so a string-based lookup could resolve to a freshly imported
    module while ``set_token_cookie`` still uses the original one.
    """
    captured: list[str] = []
    monkeypatch.setattr(ui_common.ui, "run_javascript", lambda code: captured.append(code))
    return captured


def _cookie_from_js(code: str) -> str:
    """Extract the cookie string from a ``document.cookie = "<...>"`` command."""
    prefix = "document.cookie = "
    assert code.startswith(prefix)
    assert code.endswith(";")
    return json.loads(code[len(prefix) : -1])


def _fake_https_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Context, "client", property(lambda self: _FakeClient("https")))


# =========================================================================== #
# 1. Cookie TTL + Secure (Task 35)
# =========================================================================== #


def test_set_token_cookie_http_has_max_age_no_secure(
    jwt_service: JwtService, captured_cookie_js: list[str]
) -> None:
    """Over plain HTTP: Max-Age matches the token TTL, no Secure flag."""
    token = jwt_service.create_token(user_id=1, username="alice", expires_minutes=30)
    set_token_cookie(token)

    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie
    # Max-Age matches the JWT expiry (29..30 minutes remaining).
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 29 * 60 <= max_age <= 30 * 60
    assert max_age == pytest.approx(token_max_age(token), abs=1)


def test_set_token_cookie_https_has_secure(
    jwt_service: JwtService,
    captured_cookie_js: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over HTTPS the cookie carries the Secure attribute."""
    _fake_https_client(monkeypatch)
    token = jwt_service.create_token(user_id=1, username="alice", expires_minutes=30)
    set_token_cookie(token)

    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "; Secure" in cookie


def test_clear_token_cookie_expires_immediately(captured_cookie_js: list[str]) -> None:
    """Logout clears the cookie with Max-Age=0 (over HTTP: no Secure)."""
    clear_token_cookie()
    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "Max-Age=0" in cookie
    assert "Secure" not in cookie


def test_clear_token_cookie_https_has_secure(
    captured_cookie_js: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_https_client(monkeypatch)
    clear_token_cookie()
    cookie = _cookie_from_js(captured_cookie_js[0])
    assert "Max-Age=0" in cookie
    assert "; Secure" in cookie
