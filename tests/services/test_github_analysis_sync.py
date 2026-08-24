"""tests/services/test_github_analysis_sync.py — safety-property tests for
the isolated GitHub 60-day analysis-mirror sync (never touches app.trading.*,
never network-calls in these tests -- requests.request is monkeypatched)."""
from __future__ import annotations

import json

import pytest

from app.services import github_analysis_sync as sync


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _make_archive(tmp_path, dates: list[str]):
    for d in dates:
        day_dir = tmp_path / d
        day_dir.mkdir()
        (day_dir / "flags.csv").write_text("bar_start,direction\n2026-08-01T09:00:00,UP_RED\n", encoding="utf-8")
        (day_dir / "hynix_1m.parquet").write_bytes(b"not-real-parquet-but-fine-for-hash-test")
    return tmp_path


def test_assert_allowed_rejects_paths_outside_prefix():
    with pytest.raises(ValueError):
        sync._assert_allowed("app/trading/macd2/worker.py")
    sync._assert_allowed("data/analysis_60d/20260801/flags.csv")  # must not raise


def test_plan_sync_without_token_never_calls_network(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync.requests, "request", lambda *a, **k: calls.append((a, k)) or _FakeResponse(200))
    _make_archive(tmp_path, ["20260801", "20260802"])

    plan = sync.plan_sync(archive_root=tmp_path, token=None)

    assert calls == []  # no token -> zero network calls, ever
    assert plan["error"] and "NO_TOKEN_PROVIDED" in plan["error"]
    assert set(plan["to_upload"]) == {
        "data/analysis_60d/20260801/flags.csv", "data/analysis_60d/20260801/hynix_1m.parquet",
        "data/analysis_60d/20260802/flags.csv", "data/analysis_60d/20260802/hynix_1m.parquet",
    }


def test_run_sync_dry_run_default_never_calls_write_endpoints(tmp_path, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        if method == "GET" and "/git/ref/" in url:
            return _FakeResponse(200, {"object": {"sha": "abc123"}})
        if method == "GET" and "/git/trees/" in url:
            return _FakeResponse(200, {"tree": []})
        raise AssertionError(f"dry-run must never call {method} {url}")

    monkeypatch.setattr(sync.requests, "request", fake_request)
    monkeypatch.delenv(sync.ENABLE_PUSH_ENV_VAR, raising=False)
    _make_archive(tmp_path, ["20260801"])

    result = sync.run_sync(dry_run=True, archive_root=tmp_path, token="fake-token")

    assert result["effective_dry_run"] is True
    assert "PUT" not in calls and "DELETE" not in calls
    assert result["uploads"] == [] and result["deletes"] == []
    assert result["error"] is None


def test_run_sync_stays_dry_run_even_when_caller_requests_push_without_env_flag(tmp_path, monkeypatch):
    """The two-switch requirement: dry_run=False from the CALLER is not
    enough on its own -- GITHUB_ANALYSIS_SYNC_ENABLE_PUSH must also be set."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        if method == "GET" and "/git/ref/" in url:
            return _FakeResponse(200, {"object": {"sha": "abc123"}})
        if method == "GET" and "/git/trees/" in url:
            return _FakeResponse(200, {"tree": []})
        raise AssertionError(f"must never call {method} {url} while push is not explicitly enabled")

    monkeypatch.setattr(sync.requests, "request", fake_request)
    monkeypatch.delenv(sync.ENABLE_PUSH_ENV_VAR, raising=False)
    _make_archive(tmp_path, ["20260801"])

    result = sync.run_sync(dry_run=False, archive_root=tmp_path, token="fake-token")

    assert result["effective_dry_run"] is True
    assert "PUT" not in calls and "DELETE" not in calls


def test_run_sync_pushes_only_when_both_switches_agree(tmp_path, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        if method == "GET" and "/git/ref/" in url:
            return _FakeResponse(200, {"object": {"sha": "abc123"}})
        if method == "GET" and "/git/trees/" in url:
            return _FakeResponse(200, {"tree": []})
        if method == "PUT":
            body = json.loads(kwargs["data"])
            assert body["branch"] == sync.DEFAULT_BRANCH
            return _FakeResponse(201)
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(sync.requests, "request", fake_request)
    monkeypatch.setenv(sync.ENABLE_PUSH_ENV_VAR, "true")
    _make_archive(tmp_path, ["20260801"])

    result = sync.run_sync(dry_run=False, archive_root=tmp_path, token="fake-token")

    assert result["effective_dry_run"] is False
    assert "PUT" in calls
    assert all(u["ok"] for u in result["uploads"])
    assert len(result["uploads"]) == 2  # flags.csv + hynix_1m.parquet


def test_uploads_and_deletes_never_touch_paths_outside_allowed_prefix(monkeypatch, tmp_path):
    """Defense-in-depth: even if a caller somehow constructed an out-of-prefix
    path, _upload_file/_delete_file must refuse it before any HTTP call."""
    called = []
    monkeypatch.setattr(sync.requests, "request", lambda *a, **k: called.append(1) or _FakeResponse(200))
    local = tmp_path / "x.parquet"
    local.write_bytes(b"data")

    with pytest.raises(ValueError):
        sync._upload_file("tok", sync._CallCounter(10), "app/trading/macd2/worker.py", local, None, "main")
    with pytest.raises(ValueError):
        sync._delete_file("tok", sync._CallCounter(10), "app/trading/macd2/worker.py", "sha", "main")
    assert called == []


def test_retry_cap_gives_up_after_max_retries(monkeypatch):
    import requests as requests_module

    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        raise requests_module.ConnectionError("boom")

    monkeypatch.setattr(sync.requests, "request", flaky)

    with pytest.raises(requests_module.ConnectionError):
        sync._request("GET", "https://api.github.com/x", "tok", sync._CallCounter(10))

    assert attempts["n"] == sync._MAX_RETRIES_PER_CALL


def test_call_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(sync.requests, "request", lambda *a, **k: _FakeResponse(200, {}))
    counter = sync._CallCounter(2)
    sync._request("GET", "https://api.github.com/a", "tok", counter)
    sync._request("GET", "https://api.github.com/b", "tok", counter)
    with pytest.raises(sync._CallBudgetExceeded):
        sync._request("GET", "https://api.github.com/c", "tok", counter)


def test_run_sync_never_raises_on_internal_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(sync, "_local_archived_dates", boom)

    result = sync.run_sync(dry_run=True, archive_root=tmp_path, token="fake-token")

    assert result["error"] is not None  # captured, not raised
    assert "disk exploded" in result["error"] or "RuntimeError" in result["error"]


def test_token_never_appears_in_any_result_field(tmp_path, monkeypatch):
    secret = "ghp_supersecrettoken1234567890"

    def fake_request(method, url, **kwargs):
        assert kwargs.get("headers", {}).get("Authorization") == f"Bearer {secret}"
        raise Exception(f"simulated failure while using {url}")

    monkeypatch.setattr(sync.requests, "request", fake_request)
    _make_archive(tmp_path, ["20260801"])

    result = sync.run_sync(dry_run=True, archive_root=tmp_path, token=secret)

    assert secret not in json.dumps(result)
