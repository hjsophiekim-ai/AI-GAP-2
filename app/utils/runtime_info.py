"""Runtime build/deployment metadata for operational readiness checks."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from app.utils.data_paths import STATE_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_INFO_PATH = STATE_DIR / "runtime_info.json"
SERVICE_STARTED_AT = datetime.now().isoformat()


def _git_sha(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=PROJECT_ROOT,
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").strip()
    except Exception:
        return None


def collect_runtime_info() -> dict:
    local_sha = _git_sha("rev-parse", "HEAD")
    origin_sha = _git_sha("rev-parse", "origin/main")
    render_sha = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("RENDER_COMMIT_SHA")
        or os.environ.get("GIT_SHA")
        or local_sha
    )
    build_time = (
        os.environ.get("RENDER_BUILD_TIME")
        or os.environ.get("BUILD_TIME")
        or os.environ.get("SOURCE_DATE_EPOCH")
    )
    sha_all_match = bool(local_sha and origin_sha and render_sha and local_sha == origin_sha == render_sha)
    return {
        "git_sha": local_sha,
        "origin_main_sha": origin_sha,
        "render_sha": render_sha,
        "sha_all_match": sha_all_match,
        "build_time": build_time,
        "code_path": str(PROJECT_ROOT),
        "service_started_at": SERVICE_STARTED_AT,
        "recorded_at": datetime.now().isoformat(),
        "orders_enabled_by_deployment": sha_all_match,
    }


def write_runtime_info() -> dict:
    info = collect_runtime_info()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUNTIME_INFO_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, RUNTIME_INFO_PATH)
    except Exception as exc:
        info["runtime_info_write_error"] = str(exc)
    return info


def read_runtime_info() -> dict:
    """요구사항(2026-07-21) — UI 여러 곳(상단 "Git SHA", "Local/Origin/Render SHA"
    블록, 별도 "Git commit SHA" 표시)이 각자 다른 방식으로 SHA를 구해 표시했다
    (일부는 프로세스 시작 시 캐시된 파일, 일부는 렌더링마다 새 subprocess 호출).
    그 결과 프로세스를 재시작하지 않고 같은 배포 위에서 코드가 갱신되면(예:
    수동 git pull) 캐시된 값과 실제 값이 어긋나는데도 "SHA Match=YES"가 계속
    표시될 수 있었다. 이제 이 함수 하나만 "runtime SHA"의 단일 진실 공급원으로
    쓰고, git_sha는 캐시 여부와 무관하게 항상 이번 호출 시점에 새로 조회해
    sha_all_match도 그 최신값 기준으로 재계산한다."""
    cached: dict = {}
    try:
        if RUNTIME_INFO_PATH.exists():
            cached = json.loads(RUNTIME_INFO_PATH.read_text(encoding="utf-8"))
    except Exception:
        cached = {}

    fresh_local_sha = _git_sha("rev-parse", "HEAD")
    origin_sha = cached.get("origin_main_sha") or _git_sha("rev-parse", "origin/main")
    render_sha = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("RENDER_COMMIT_SHA")
        or os.environ.get("GIT_SHA")
        or cached.get("render_sha")
        or fresh_local_sha
    )
    sha_all_match = bool(fresh_local_sha and origin_sha and render_sha and fresh_local_sha == origin_sha == render_sha)
    info = {
        **cached,
        "git_sha": fresh_local_sha or cached.get("git_sha"),
        "origin_main_sha": origin_sha,
        "render_sha": render_sha,
        "sha_all_match": sha_all_match,
        "orders_enabled_by_deployment": sha_all_match,
        "checked_at": datetime.now().isoformat(),
    }
    if not cached:
        return collect_runtime_info()
    return info
