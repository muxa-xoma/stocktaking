"""Smoke test: verify package imports and config loading."""

from bond_accounting.config.settings import Settings


def test_import_package() -> None:
    import bond_accounting

    assert hasattr(bond_accounting, "__name__")


def test_settings_model() -> None:
    s = Settings(
        auth={"jwt_secret": "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"},  # type: ignore[arg-type]  # pydantic coerces nested dicts
    )
    assert s.app.port == 8080
    assert s.database.url == "sqlite+aiosqlite:///data/bond_accounting.db"
    assert s.auth.jwt_secret == "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
