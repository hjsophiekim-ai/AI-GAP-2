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
    try:
        if RUNTIME_INFO_PATH.exists():
            return json.loads(RUNTIME_INFO_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return collect_runtime_info()
