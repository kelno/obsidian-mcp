"""Verifies the startup dimension-mismatch check exits non-zero on mismatch."""
import pytest

from src import main as main_module


class _FakeResult:
    def __init__(self, atttypmod: int | None):
        self._row = (atttypmod,) if atttypmod is not None else None

    def first(self):
        return self._row


class _FakeSession:
    """Every vector column (notes' and the agent ledger's) answers the same."""

    def __init__(self, atttypmod: int | None):
        self._atttypmod = atttypmod

    async def execute(self, _stmt, _params=None):
        return _FakeResult(self._atttypmod)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _patch_session(monkeypatch, atttypmod: int | None):
    monkeypatch.setattr(
        main_module,
        "async_session",
        lambda: _FakeSession(atttypmod),
    )


@pytest.mark.asyncio
async def test_dim_match_passes(monkeypatch):
    monkeypatch.setattr(main_module.settings, "embedding_dimensions", 1024)
    _patch_session(monkeypatch, 1024)
    # Should not raise / sys.exit
    await main_module._check_embedding_dim()


@pytest.mark.asyncio
async def test_dim_mismatch_exits(monkeypatch):
    monkeypatch.setattr(main_module.settings, "embedding_dimensions", 1536)
    _patch_session(monkeypatch, 1024)

    exit_calls: list[int] = []

    def _fake_exit(code: int = 0):
        exit_calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(main_module.sys, "exit", _fake_exit)

    with pytest.raises(SystemExit):
        await main_module._check_embedding_dim()
    assert exit_calls == [1]


@pytest.mark.asyncio
async def test_table_missing_does_not_exit(monkeypatch):
    """If the table hasn't been created yet, defer to alembic — don't exit."""
    monkeypatch.setattr(main_module.settings, "embedding_dimensions", 1024)
    _patch_session(monkeypatch, None)
    await main_module._check_embedding_dim()
