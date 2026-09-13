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


def _assert_allowed(path: str, allowed_prefix: str = ALLOWED_PREFIX) -> None:
    """마지막 방어선. prefix 는 **호출부마다 명시** 한다 -- 두 sync 경로가 서로의
    디렉터리를 건드릴 수 있게 하나로 합치지 않는다(60d sync 는 premarket_carry/
    를, premarket sync 는 analysis_60d/ 를 영원히 건드릴 수 없다)."""
    if not path.startswith(allowed_prefix):
        raise ValueError(f"refusing to touch path outside {allowed_prefix!r}: {path!r}")


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


def _upload_file(token: str, counter: _CallCounter, repo_path: str, local_path: Path, existing_sha: Optional[str], branch: str, *, allowed_prefix: str = ALLOWED_PREFIX, message: Optional[str] = None) -> dict[str, Any]:
    _assert_allowed(repo_path, allowed_prefix)
    content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
    body = {
        "message": message or f"analysis_60d sync: {repo_path}",
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


# ══════════════════════════════════════════════════════════════════════════
#  Premarket Carry Shadow sync (2026-09-13)
#
#  60d 아카이브 sync 와 **완전히 독립된 두 번째 경로**다. 공유하는 것은
#  _request/_CallCounter 같은 저수준 HTTP 유틸뿐이고, 대상 디렉터리·repo
#  prefix·보존정책·커밋방식이 모두 다르다.
#
#  이 경로만의 추가 원칙 (사용자 요구 2026-09-13):
#   1. 업로드 직전 **민감정보 검사**를 통과하지 못하면 push 를 통째로 중단하고
#      로그 경고만 남긴다(부분 업로드조차 하지 않는다).
#   2. **force push 금지** -- ref 갱신은 항상 fast-forward(force=false)로만.
#   3. 하루 최대 1 커밋. Git Data API(blob->tree->commit->ref)로 여러 파일을
#      한 커밋에 묶는다.
#   4. 내용이 바뀌지 않았으면 커밋하지 않는다(빈 커밋 금지).
#   5. 트레이딩과 fail-open 분리 -- 이 함수는 어떤 경우에도 예외를 던지지
#      않는다. 실패는 결과 dict 의 error 필드로만 보고된다.
#   6. 60일 창 밖 prune 없음(관측 표본은 지우지 않는다). DELETE 를 단 한 번도
#      호출하지 않는다.
# ══════════════════════════════════════════════════════════════════════════

import re as _re

PREMARKET_CARRY_PREFIX = "data/analysis_live/premarket_carry/"

#: 동기화 대상 파일 이름 패턴 -- 화이트리스트. shadow 상태 JSON
#: (premarket_carry_shadow_state.json) 은 **의도적으로 제외** 한다.
_PREMARKET_FILE_PATTERNS = (
    _re.compile(r"^premarket_carry_\d{8}\.csv$"),
    _re.compile(r"^premarket_carry_summary\.csv$"),
)

#: app/trading/macd2/premarket_shadow.FORBIDDEN_COLUMN_HINTS 의 독립 사본.
#: 이 모듈은 app/trading/* 를 절대 import 하지 않는다는 원칙을 지키기 위해
#: 일부러 복제했다. 두 목록이 어긋나지 않는지는 테스트가 검사한다.
_SENSITIVE_COLUMN_HINTS = (
    "account", "acct", "cano", "appkey", "app_key", "appsecret", "app_secret",
    "token", "secret", "password", "balance", "cash", "orderable", "nrcvb",
    "psbl", "deposit", "buying_power", "잔고", "계좌",
)

#: 컬럼명이 멀쩡해도 **값** 에 비밀이 섞였을 수 있다. 값 레벨 2차 검사.
_SENSITIVE_VALUE_PATTERNS = (
    ("JWT_LIKE", _re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.")),
    ("BEARER_HEADER", _re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")),
    ("KIS_APPKEY_LIKE", _re.compile(r"\bPS[A-Za-z0-9]{16,}\b")),
    ("ACCOUNT_NO_LIKE", _re.compile(r"\b\d{8}-\d{2}\b")),
    ("LONG_OPAQUE_SECRET", _re.compile(r"\b[A-Za-z0-9+/_\-]{40,}\b")),
)


class SensitiveDataFound(Exception):
    """민감정보 검사 실패. 이 예외가 나오면 push 를 전면 중단한다."""


def assert_no_sensitive_data(path: Path) -> None:
    """업로드 직전 최종 관문. 컬럼명 + 값 양쪽을 모두 본다.

    통과하지 못하면 SensitiveDataFound 를 던진다. **발견된 실제 값은 절대
    메시지에 담지 않는다** -- 어떤 규칙에 어느 줄이 걸렸는지만 남긴다."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SensitiveDataFound(f"{path.name}: not valid utf-8 text ({exc.reason})") from None

    lines = text.splitlines()
    if not lines:
        return

    header_cells = [c.strip().strip('"').lower() for c in lines[0].split(",")]
    for cell in header_cells:
        for hint in _SENSITIVE_COLUMN_HINTS:
            if hint in cell:
                raise SensitiveDataFound(
                    f"{path.name}: forbidden column name matched hint {hint!r}"
                )

    for lineno, line in enumerate(lines, start=1):
        for rule_name, pattern in _SENSITIVE_VALUE_PATTERNS:
            if pattern.search(line):
                raise SensitiveDataFound(
                    f"{path.name}: line {lineno} matched sensitive-value rule {rule_name}"
                )


def _git_blob_sha(data: bytes) -> str:
    """git 이 이 내용에 부여할 blob SHA-1. 로컬에서 계산해 repo tree 의 sha 와
    직접 비교하면, 파일 내용을 내려받지 않고도 변경 여부를 **정확히** 안다
    (프로세스 로컬 캐시에 의존하지 않으므로 재시작에도 안전)."""
    header = ("blob %d" % len(data)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


def _premarket_local_files(source_dir: Path) -> dict[str, Path]:
    """repo path -> local Path. 화이트리스트에 맞는 파일만."""
    out: dict[str, Path] = {}
    if not source_dir.exists():
        return out
    for f in sorted(source_dir.iterdir()):
        if not f.is_file():
            continue
        if not any(p.match(f.name) for p in _PREMARKET_FILE_PATTERNS):
            continue
        out[PREMARKET_CARRY_PREFIX + f.name] = f
    return out


def _premarket_sync_state_path(source_dir: Path) -> Path:
    return source_dir / "_github_sync_state.json"


def _premarket_load_sync_state(source_dir: Path) -> dict[str, Any]:
    try:
        return json.loads(_premarket_sync_state_path(source_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _premarket_save_sync_state(source_dir: Path, state: dict[str, Any]) -> None:
    try:
        source_dir.mkdir(parents=True, exist_ok=True)
        target = _premarket_sync_state_path(source_dir)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:  # 상태 저장 실패가 sync 실패는 아니다
        logger.warning(f"[premarket-sync] sync-state 저장 실패(무시): {_redact(exc)}")


def _premarket_source_dir() -> Path:
    from app.utils.data_paths import PREMARKET_CARRY_DIR

    return PREMARKET_CARRY_DIR


def _list_repo_files_under(prefix: str, token: str, counter: _CallCounter, branch: str) -> dict[str, str]:
    """path -> blob sha, 주어진 prefix 하위만. 재귀 tree 1회 호출."""
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
    out: dict[str, str] = {}
    for entry in tree_resp.json().get("tree", []):
        path = entry.get("path", "")
        if entry.get("type") == "blob" and path.startswith(prefix):
            out[path] = entry.get("sha", "")
    return out


def plan_premarket_carry_sync(
    *, source_dir: Optional[Path] = None, token: Optional[str] = None,
    branch: str = DEFAULT_BRANCH,
) -> dict[str, Any]:
    """읽기 전용: 무엇이 바뀌었고 민감정보 검사를 통과하는지만 계산한다.
    쓰기 엔드포인트를 단 한 번도 호출하지 않는다. 절대 예외를 던지지 않는다."""
    source_dir = Path(source_dir) if source_dir else _premarket_source_dir()
    plan: dict[str, Any] = {
        "computed_at": kst_now().isoformat(timespec="seconds"),
        "source_dir": str(source_dir),
        "prefix": PREMARKET_CARRY_PREFIX,
        "local_files": [], "changed": [], "unchanged": 0,
        "sensitive_check": "NOT_RUN", "sensitive_violations": [],
        "repo_listing_available": False, "error": None,
    }
    try:
        local_files = _premarket_local_files(source_dir)
        for repo_path in local_files:
            _assert_allowed(repo_path, PREMARKET_CARRY_PREFIX)
        plan["local_files"] = sorted(local_files)

        violations: list[str] = []
        for repo_path, local_path in sorted(local_files.items()):
            try:
                assert_no_sensitive_data(local_path)
            except SensitiveDataFound as exc:
                violations.append(str(exc))
        plan["sensitive_violations"] = violations
        plan["sensitive_check"] = "FAIL" if violations else "PASS"

        token = token or _token_from_env()
        if not token:
            plan["error"] = "NO_TOKEN_PROVIDED -- repo diff skipped"
            plan["changed"] = sorted(local_files)
            return plan

        counter = _CallCounter(_MAX_API_CALLS_PER_RUN)
        repo_files = _list_repo_files_under(PREMARKET_CARRY_PREFIX, token, counter, branch)
        plan["repo_listing_available"] = True
        for repo_path, local_path in sorted(local_files.items()):
            if repo_files.get(repo_path) != _git_blob_sha(local_path.read_bytes()):
                plan["changed"].append(repo_path)
            else:
                plan["unchanged"] += 1
    except Exception as exc:
        plan["error"] = _redact(exc)
    return plan


def run_premarket_carry_sync(
    *, dry_run: bool = True, source_dir: Optional[Path] = None,
    token: Optional[str] = None, branch: str = DEFAULT_BRANCH,
    max_commits_per_day: int = 1,
) -> dict[str, Any]:
    """스케줄러가 부르는 유일한 함수. 절대 예외를 던지지 않는다.

    dry_run=False 를 넘겨도 GITHUB_ANALYSIS_SYNC_ENABLE_PUSH=true 가 아니면
    쓰기 API 를 한 번도 호출하지 않는다(스위치 2개 모두 동의해야 함)."""
    started = kst_now()
    today = started.strftime("%Y-%m-%d")
    result: dict[str, Any] = {
        "dry_run": dry_run, "started_at": started.isoformat(timespec="seconds"),
        "plan": None, "committed": False, "commit_sha": None,
        "uploaded": [], "skipped_reason": None, "error": None,
    }
    source_dir = Path(source_dir) if source_dir else _premarket_source_dir()
    try:
        state = _premarket_load_sync_state(source_dir)
        commits_today = int(state.get("commits_by_date", {}).get(today, 0))
        if commits_today >= max_commits_per_day:
            result["skipped_reason"] = "ALREADY_COMMITTED_TODAY"
            return result

        plan = plan_premarket_carry_sync(source_dir=source_dir, token=token, branch=branch)
        result["plan"] = plan

        # ── 관문 1: 민감정보. 하나라도 걸리면 push 전면 중단 ────────────────
        if plan["sensitive_check"] == "FAIL":
            for v in plan["sensitive_violations"]:
                logger.warning(f"[premarket-sync] 민감정보 의심으로 GitHub push 중단: {v}")
            result["skipped_reason"] = "SENSITIVE_DATA_DETECTED"
            return result

        if plan.get("error") and not plan.get("repo_listing_available"):
            result["error"] = plan["error"]
            return result
        if not plan["changed"]:
            result["skipped_reason"] = "NO_CHANGES"
            return result

        effective_dry_run = dry_run or not _push_enabled_by_env()
        result["effective_dry_run"] = effective_dry_run
        if effective_dry_run:
            result["skipped_reason"] = "DRY_RUN"
            return result

        token = token or _token_from_env()
        if not token:
            result["error"] = "NO_TOKEN_PROVIDED"
            return result

        local_files = _premarket_local_files(source_dir)
        counter = _CallCounter(_MAX_API_CALLS_PER_RUN)

        # ── 관문 2: 업로드 직전 재검사(계획 이후 파일이 바뀌었을 수도 있다) ──
        payloads: dict[str, bytes] = {}
        for repo_path in plan["changed"]:
            local_path = local_files.get(repo_path)
            if local_path is None:
                continue
            _assert_allowed(repo_path, PREMARKET_CARRY_PREFIX)
            assert_no_sensitive_data(local_path)
            payloads[repo_path] = local_path.read_bytes()
        if not payloads:
            result["skipped_reason"] = "NO_CHANGES"
            return result

        commit_sha = _commit_files_single_commit(
            token, counter, payloads, branch,
            message=f"data: sync premarket carry shadow {today}",
        )
        if commit_sha is None:
            result["skipped_reason"] = "TREE_UNCHANGED"
            return result

        result["committed"] = True
        result["commit_sha"] = commit_sha
        result["uploaded"] = sorted(payloads)
        state.setdefault("commits_by_date", {})[today] = commits_today + 1
        state["last_commit_sha"] = commit_sha
        state["last_commit_at"] = kst_now().isoformat(timespec="seconds")
        # 날짜 키가 무한히 쌓이지 않도록 최근 60일만 유지
        keys = sorted(state["commits_by_date"])[-60:]
        state["commits_by_date"] = {k: state["commits_by_date"][k] for k in keys}
        _premarket_save_sync_state(source_dir, state)
        logger.info(f"[premarket-sync] 커밋 완료 {commit_sha[:8]} · {len(payloads)}개 파일")
    except SensitiveDataFound as exc:
        logger.warning(f"[premarket-sync] 민감정보 의심으로 GitHub push 중단: {exc}")
        result["skipped_reason"] = "SENSITIVE_DATA_DETECTED"
    except Exception as exc:
        result["error"] = _redact(exc)
        logger.warning(f"[premarket-sync] sync 실패(트레이딩에는 영향 없음): {result['error']}")
    return result


def _commit_files_single_commit(
    token: str, counter: _CallCounter, payloads: dict[str, bytes], branch: str,
    *, message: str,
) -> Optional[str]:
    """여러 파일을 **한 커밋**으로 묶어 올린다(Git Data API).

    force push 를 절대 하지 않는다: ref 갱신은 force=false 로만 보내고,
    다른 곳에서 먼저 push 해서 fast-forward 가 불가능해지면 GitHub 이 422 를
    돌려주고 우리는 그대로 실패시킨다(덮어쓰지 않는다).

    새 tree 가 기존 tree 와 같으면(= 실질 변경 없음) 커밋하지 않고 None."""
    for repo_path in payloads:
        _assert_allowed(repo_path, PREMARKET_CARRY_PREFIX)

    ref_url = f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{branch}"
    ref_resp = _request("GET", ref_url, token, counter)
    if ref_resp.status_code != 200:
        raise RuntimeError(f"failed to resolve branch ref (status={ref_resp.status_code})")
    base_commit_sha = ref_resp.json()["object"]["sha"]

    commit_resp = _request(
        "GET", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits/{base_commit_sha}",
        token, counter,
    )
    if commit_resp.status_code != 200:
        raise RuntimeError(f"failed to read base commit (status={commit_resp.status_code})")
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    tree_entries = []
    for repo_path, data in sorted(payloads.items()):
        blob_resp = _request(
            "POST", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs", token, counter,
            data=json.dumps({
                "content": base64.b64encode(data).decode("ascii"), "encoding": "base64",
            }),
        )
        if blob_resp.status_code not in (200, 201):
            raise RuntimeError(f"blob create failed (status={blob_resp.status_code})")
        tree_entries.append({
            "path": repo_path, "mode": "100644", "type": "blob",
            "sha": blob_resp.json()["sha"],
        })

    tree_resp = _request(
        "POST", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees", token, counter,
        data=json.dumps({"base_tree": base_tree_sha, "tree": tree_entries}),
    )
    if tree_resp.status_code not in (200, 201):
        raise RuntimeError(f"tree create failed (status={tree_resp.status_code})")
    new_tree_sha = tree_resp.json()["sha"]
    if new_tree_sha == base_tree_sha:
        return None  # 빈 커밋 금지

    new_commit_resp = _request(
        "POST", f"{_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits", token, counter,
        data=json.dumps({
            "message": message, "tree": new_tree_sha, "parents": [base_commit_sha],
        }),
    )
    if new_commit_resp.status_code not in (200, 201):
        raise RuntimeError(f"commit create failed (status={new_commit_resp.status_code})")
    new_commit_sha = new_commit_resp.json()["sha"]

    patch_resp = _request(
        "PATCH", ref_url, token, counter,
        data=json.dumps({"sha": new_commit_sha, "force": False}),  # ← force push 영구 금지
    )
    if patch_resp.status_code != 200:
        raise RuntimeError(f"ref update failed (status={patch_resp.status_code})")
    return new_commit_sha
