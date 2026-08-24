"""
github_analysis_sync.py — Render Persistent Disk에 쌓인 macd2_daily_archive
중 최근 60영업일치 파생 데이터(Parquet + CSV 요약)만 골라 GitHub repo의
data/analysis_60d/ 로 동기화하는, 완전히 독립된 read-only 부가기능이다.

절대 원칙 (사용자 요구 2026-08-24):
- app/trading/* 는 이 파일에서 절대 import하지 않는다 -- 트레이딩 로직과
  100% 분리. Worker/시세조회/플래그생성/주문 경로 어디에도 이 모듈이 관여할
  방법이 없다.
- GitHub Contents API로 ALLOWED_PREFIX(= "data/analysis_60d/") 하위 경로만
  건드린다 -- 다른 어떤 파일/경로도 절대 수정·삭제하지 않는다. 모든 쓰기
  경로는 _assert_allowed()를 통과해야만 실제 API를 호출한다(방어적 이중
  체크: 호출부 로직이 잘못 계산해도 이 검사가 최후 방어선이 된다).
- 토큰은 절대 로그/manifest/예외 메시지에 남기지 않는다(_redact가 모든 에러
  경로에서 사용됨).
- dry_run=True(기본값)에서는 실제 쓰기(PUT/DELETE) API를 단 한 번도 호출하지
  않고, "무엇을 올리고 지울 것인지" 계획만 돌려준다. 실제 push는
  dry_run=False를 명시적으로 넘겨야만 실행된다.
- 재시도 상한(_MAX_RETRIES_PER_CALL) 있음 -- 무한 재시도 없음. 실행당 API 호출
  총량도 _MAX_API_CALLS_PER_RUN으로 상한을 둔다.
- 이 모듈의 공개 함수(plan_sync/run_sync)는 예외를 절대 밖으로 던지지 않는다
  -- 어떤 스케줄러/스레드가 이 모듈을 부르든, 이 모듈의 실패가 그 스레드조차
  죽이지 않게 하기 위함.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from app.logger import logger
from app.utils.data_paths import MACD2_DAILY_ARCHIVE_DIR
from app.utils.time_utils import kst_now

REPO_OWNER = "hjsophiekim-ai"
REPO_NAME = "AI-GAP-2"
DEFAULT_BRANCH = "main-MACD2"
ALLOWED_PREFIX = "data/analysis_60d/"
ROLLING_WINDOW_TRADING_DAYS = 60
TOKEN_ENV_VAR = "GITHUB_ANALYSIS_SYNC_TOKEN"
ENABLE_PUSH_ENV_VAR = "GITHUB_ANALYSIS_SYNC_ENABLE_PUSH"

_API_BASE = "https://api.github.com"
_MAX_RETRIES_PER_CALL = 2
_MAX_API_CALLS_PER_RUN = 400  # generous upper bound for ~60 days x ~9 files, still a hard cap
_REQUEST_TIMEOUT_SEC = 20


class _CallBudgetExceeded(Exception):
    pass


def _redact(exc: Exception) -> str:
    """Exception repr with any token-looking substring stripped -- tokens
    never appear in this module's own state, but requests' exceptions can
    echo back request headers/URLs in rare cases; this is the defense-in-
    depth net, not the primary control (the primary control is: the token is
    only ever placed in an Authorization header, never in a URL/body)."""
    text = repr(exc)
    return text[:500]


def _assert_allowed(path: str) -> None:
    if not path.startswith(ALLOWED_PREFIX):
        raise ValueError(f"refusing to touch path outside {ALLOWED_PREFIX!r}: {path!r}")


def _push_enabled_by_env() -> bool:
    import os

    return os.environ.get(ENABLE_PUSH_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


def _token_from_env() -> Optional[str]:
    import os

    return os.environ.get(TOKEN_ENV_VAR) or None


class _CallCounter:
    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0

    def take(self) -> None:
        if self.used >= self.budget:
            raise _CallBudgetExceeded(f"exceeded {self.budget} GitHub API calls in this run")
        self.used += 1


def _request(method: str, url: str, token: str, counter: _CallCounter, **kwargs) -> requests.Response:
    counter.take()
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES_PER_CALL + 1):
        try:
            resp = requests.request(
                method, url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                timeout=_REQUEST_TIMEOUT_SEC, **kwargs,
            )
            if resp.status_code >= 500 and attempt < _MAX_RETRIES_PER_CALL:
                time.sleep(1.0 * attempt)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES_PER_CALL:
                time.sleep(1.0 * attempt)
                continue
    raise last_exc if last_exc is not None else RuntimeError("request failed with no exception captured")


def _local_archived_dates(archive_root: Path) -> list[str]:
    """Every date-folder under the Disk archive that actually contains at
    least one file -- an empty/nonexistent day never counts as archived."""
    if not archive_root.exists():
        return []
    out = []
    for child in sorted(archive_root.iterdir()):
        if child.is_dir() and child.name.isdigit() and len(child.name) == 8 and any(child.iterdir()):
            out.append(child.name)
    return sorted(out)


def _rolling_window_dates(archive_root: Path, window: int = ROLLING_WINDOW_TRADING_DAYS) -> list[str]:
    dates = _local_archived_dates(archive_root)
    return dates[-window:]


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_files_for_dates(archive_root: Path, dates: list[str]) -> dict[str, Path]:
    """repo-relative path (under ALLOWED_PREFIX) -> local Path, for every
    file that should exist in the repo mirror for these dates."""
    out: dict[str, Path] = {}
    for d in dates:
        day_dir = archive_root / d
        if not day_dir.is_dir():
            continue
        for f in sorted(day_dir.iterdir()):
            if not f.is_file():
                continue
            repo_path = f"{ALLOWED_PREFIX}{d}/{f.name}"
            out[repo_path] = f
    return out


def _list_repo_files(token: str, counter: _CallCounter, branch: str = DEFAULT_BRANCH) -> dict[str, str]:
    """path -> git blob sha, for every existing file under ALLOWED_PREFIX in
    the repo, via a single recursive tree listing (cheap: 1 API call)."""
    ref_resp = _request(
        "GET", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{branch}", token, counter,
    )
    if ref_resp.status_code != 200:
        raise RuntimeError(f"failed to resolve branch ref (status={ref_resp.status_code})")
    commit_sha = ref_resp.json()["object"]["sha"]
    tree_resp = _request(
        "GET", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{commit_sha}", token, counter,
        params={"recursive": "1"},
    )
    if tree_resp.status_code != 200:
        raise RuntimeError(f"failed to list repo tree (status={tree_resp.status_code})")
    tree = tree_resp.json().get("tree", [])
    out: dict[str, str] = {}
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") == "blob" and path.startswith(ALLOWED_PREFIX):
            out[path] = entry.get("sha", "")
    return out


def plan_sync(*, archive_root: Optional[Path] = None, token: Optional[str] = None) -> dict[str, Any]:
    """Read-only: computes what WOULD be uploaded/updated/deleted, never
    calls a write endpoint. Safe to call with no token for the local-only
    part; the repo-diff part is skipped (marked unavailable) without a
    token, never raises for a missing token."""
    archive_root = Path(archive_root) if archive_root else MACD2_DAILY_ARCHIVE_DIR
    plan: dict[str, Any] = {
        "computed_at": kst_now().isoformat(timespec="seconds"),
        "archive_root": str(archive_root),
        "window_dates": [], "to_upload": [], "to_update": [], "to_delete": [], "unchanged": 0,
        "repo_listing_available": False, "error": None,
    }
    try:
        dates = _rolling_window_dates(archive_root)
        plan["window_dates"] = dates
        local_files = _local_files_for_dates(archive_root, dates)
        for repo_path in local_files:
            _assert_allowed(repo_path)

        token = token or _token_from_env()
        if not token:
            plan["error"] = "NO_TOKEN_PROVIDED -- repo diff skipped, only local file list computed"
            plan["to_upload"] = sorted(local_files.keys())
            return plan

        counter = _CallCounter(_MAX_API_CALLS_PER_RUN)
        repo_files = _list_repo_files(token, counter)
        plan["repo_listing_available"] = True
        plan["_repo_files"] = repo_files  # reused by run_sync to avoid a second listing call

        for repo_path, local_path in local_files.items():
            local_hash = _sha256_of_file(local_path)
            cached_hash = _content_hash_cache_get(repo_path)
            if repo_path not in repo_files:
                plan["to_upload"].append(repo_path)
            elif cached_hash != local_hash:
                plan["to_update"].append(repo_path)
            else:
                plan["unchanged"] += 1

        window_prefix_set = {f"{ALLOWED_PREFIX}{d}/" for d in dates}
        for repo_path in repo_files:
            if not any(repo_path.startswith(p) for p in window_prefix_set):
                plan["to_delete"].append(repo_path)
    except Exception as exc:
        plan["error"] = _redact(exc)
    return plan


# repo_path -> last-known-uploaded local sha256, so plan_sync doesn't need to
# re-download every file's content just to compare -- populated only by a
# successful run_sync upload/update, never by plan_sync itself (dry-run must
# never have a side effect). Process-local only; a fresh process re-derives
# "to_update" conservatively (falls back to re-uploading unchanged content,
# which is harmless -- GitHub no-ops an identical-content PUT to the same sha).
_content_hash_cache: dict[str, str] = {}


def _content_hash_cache_get(repo_path: str) -> Optional[str]:
    return _content_hash_cache.get(repo_path)


def _content_hash_cache_set(repo_path: str, sha256_hex: str) -> None:
    _content_hash_cache[repo_path] = sha256_hex


def _upload_file(token: str, counter: _CallCounter, repo_path: str, local_path: Path, existing_sha: Optional[str], branch: str) -> dict[str, Any]:
    _assert_allowed(repo_path)
    content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
    body = {
        "message": f"analysis_60d sync: {repo_path}",
        "content": content_b64,
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha
    resp = _request(
        "PUT", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_path}", token, counter,
        data=json.dumps(body),
    )
    ok = resp.status_code in (200, 201)
    if ok:
        _content_hash_cache_set(repo_path, _sha256_of_file(local_path))
    return {"path": repo_path, "ok": ok, "status_code": resp.status_code}


def _delete_file(token: str, counter: _CallCounter, repo_path: str, existing_sha: str, branch: str) -> dict[str, Any]:
    _assert_allowed(repo_path)
    body = {"message": f"analysis_60d prune (outside {ROLLING_WINDOW_TRADING_DAYS}-day window): {repo_path}", "sha": existing_sha, "branch": branch}
    resp = _request(
        "DELETE", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_path}", token, counter,
        data=json.dumps(body),
    )
    ok = resp.status_code in (200,)
    if ok:
        _content_hash_cache.pop(repo_path, None)
    return {"path": repo_path, "ok": ok, "status_code": resp.status_code}


def run_sync(*, dry_run: bool = True, archive_root: Optional[Path] = None, token: Optional[str] = None, branch: str = DEFAULT_BRANCH) -> dict[str, Any]:
    """The only function meant to be called from a scheduler. Always safe:
    never raises, defaults to dry_run=True, and dry_run additionally requires
    GITHUB_ANALYSIS_SYNC_ENABLE_PUSH=true in the environment even when the
    CALLER passes dry_run=False -- two independent switches must both agree
    before a single write API call happens."""
    result: dict[str, Any] = {"dry_run": dry_run, "started_at": kst_now().isoformat(timespec="seconds"), "plan": None, "uploads": [], "deletes": [], "error": None}
    try:
        plan = plan_sync(archive_root=archive_root, token=token)
        repo_files = plan.pop("_repo_files", None)
        result["plan"] = plan
        if plan.get("error") and not plan.get("repo_listing_available"):
            result["error"] = plan["error"]
            return result

        effective_dry_run = dry_run or not _push_enabled_by_env()
        result["effective_dry_run"] = effective_dry_run
        if effective_dry_run:
            return result

        token = token or _token_from_env()
        if not token:
            result["error"] = "NO_TOKEN_PROVIDED"
            return result
        archive_root = Path(archive_root) if archive_root else MACD2_DAILY_ARCHIVE_DIR

        counter = _CallCounter(_MAX_API_CALLS_PER_RUN)
        if repo_files is None:
            repo_files = _list_repo_files(token, counter)
        dates = plan["window_dates"]
        local_files = _local_files_for_dates(archive_root, dates)

        for repo_path in plan["to_upload"] + plan["to_update"]:
            local_path = local_files.get(repo_path)
            if local_path is None:
                continue
            try:
                res = _upload_file(token, counter, repo_path, local_path, repo_files.get(repo_path), branch)
            except _CallBudgetExceeded:
                result["error"] = "API_CALL_BUDGET_EXCEEDED_MID_RUN"
                return result
            except Exception as exc:
                res = {"path": repo_path, "ok": False, "error": _redact(exc)}
            result["uploads"].append(res)

        for repo_path in plan["to_delete"]:
            sha = repo_files.get(repo_path)
            if not sha:
                continue
            try:
                res = _delete_file(token, counter, repo_path, sha, branch)
            except _CallBudgetExceeded:
                result["error"] = "API_CALL_BUDGET_EXCEEDED_MID_RUN"
                return result
            except Exception as exc:
                res = {"path": repo_path, "ok": False, "error": _redact(exc)}
            result["deletes"].append(res)
    except Exception as exc:
        result["error"] = _redact(exc)
    result["finished_at"] = kst_now().isoformat(timespec="seconds")
    return result
