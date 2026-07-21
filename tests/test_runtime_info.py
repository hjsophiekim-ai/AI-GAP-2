"""
test_runtime_info.py — read_runtime_info()가 SHA 표시의 단일 진실 공급원인지 검증
(2026-07-21 — 상단 Git SHA와 Local/Origin/Render SHA가 서로 다른 시점 값이라
SHA Match=YES가 실제와 어긋날 수 있던 문제 수정).
"""

from __future__ import annotations

import json

from app.utils import runtime_info


def test_read_runtime_info_always_uses_fresh_local_sha_not_stale_cache(tmp_path, monkeypatch):
    """캐시 파일에 오래된(다른) git_sha가 저장돼 있어도, read_runtime_info()는
    항상 지금 이 순간의 실제 로컬 HEAD를 다시 조회해 써야 한다."""
    cache_path = tmp_path / "runtime_info.json"
    cache_path.write_text(json.dumps({
        "git_sha": "stale0000000000000000000000000000000000",
        "origin_main_sha": "stale0000000000000000000000000000000000",
        "render_sha": "stale0000000000000000000000000000000000",
        "sha_all_match": True,
        "build_time": None, "code_path": "x", "service_started_at": "x", "recorded_at": "x",
        "orders_enabled_by_deployment": True,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_info, "RUNTIME_INFO_PATH", cache_path)
    monkeypatch.setattr(runtime_info, "_git_sha", lambda *args: "fresh1111111111111111111111111111111111")

    info = runtime_info.read_runtime_info()
    assert info["git_sha"] == "fresh1111111111111111111111111111111111"


def test_sha_mismatch_is_reported_as_no_even_if_cache_said_yes(tmp_path, monkeypatch):
    cache_path = tmp_path / "runtime_info.json"
    cache_path.write_text(json.dumps({
        "git_sha": "same0000000000000000000000000000000000",
        "origin_main_sha": "same0000000000000000000000000000000000",
        "render_sha": "same0000000000000000000000000000000000",
        "sha_all_match": True,
        "orders_enabled_by_deployment": True,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_info, "RUNTIME_INFO_PATH", cache_path)
    # 로컬 HEAD가 캐시된 값과 실제로 달라졌다(예: 재시작 없이 코드만 갱신됨).
    monkeypatch.setattr(runtime_info, "_git_sha", lambda *args: "different11111111111111111111111111111")

    info = runtime_info.read_runtime_info()
    assert info["sha_all_match"] is False
    assert info["orders_enabled_by_deployment"] is False


def test_sha_match_true_when_all_three_actually_agree(tmp_path, monkeypatch):
    cache_path = tmp_path / "runtime_info.json"
    cache_path.write_text(json.dumps({
        "git_sha": "irrelevant",
        "origin_main_sha": "abc123",
        "render_sha": "abc123",
        "sha_all_match": False,
        "orders_enabled_by_deployment": False,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_info, "RUNTIME_INFO_PATH", cache_path)
    monkeypatch.setattr(runtime_info, "_git_sha", lambda *args: "abc123")

    info = runtime_info.read_runtime_info()
    assert info["git_sha"] == "abc123"
    assert info["sha_all_match"] is True
    assert info["orders_enabled_by_deployment"] is True
