"""
data_paths.py — 모든 런타임 파일 저장 경로의 단일 진실 공급원(Single Source of Truth).

Render 같은 배포 환경은 컨테이너 로컬 파일시스템이 기본적으로 임시(ephemeral)라,
재배포·재시작·(무료/스타터 플랜의) 자동 슬립마다 로컬에 쓴 파일이 전부 사라진다.
거래원장(execution ledger)/계좌 상태(state)/스케줄러 heartbeat처럼 재시작 후에도
반드시 남아있어야 하는 파일이 컨테이너 로컬 경로("data/...")에 하드코딩돼 있으면,
Render Persistent Disk를 붙여도 그 디스크가 마운트된 경로가 아니라 컨테이너
로컬(휘발성) 경로에 계속 쓰게 되어 여전히 유실된다(2026-07-16 실측 — 실제
체결/포지션은 있는데 거래횟수/실현손익/거래원장이 전부 0/빈 값으로 보임).

이 모듈은 환경변수 AI_GAP_DATA_DIR(Render에서 Persistent Disk 마운트 경로,
예: /opt/render/project/src/data)을 데이터 루트로 쓰고, 설정돼 있지 않으면
기존과 동일하게 프로젝트 루트의 data/ 디렉토리를 기본값으로 쓴다. 프로젝트 안의
다른 모든 모듈은 "data/..." 상대경로나 자체 ROOT 계산을 새로 하지 말고, 반드시
이 모듈이 제공하는 경로 상수/헬퍼만 사용해야 한다.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 프로젝트 루트(코드가 checkout된 위치) — 데이터 루트의 "기본값" 계산에만 쓰인다.
# 실제 데이터가 어디 저장되는지는 DATA_ROOT(아래)만 봐야 한다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DATA_ROOT_ENV_VAR = "AI_GAP_DATA_DIR"


def _resolve_data_root() -> Path:
    env_value = os.environ.get(DATA_ROOT_ENV_VAR)
    if env_value:
        return Path(env_value)
    return PROJECT_ROOT / "data"


# 모듈 로드 시점에 한 번 확정한다 — 같은 프로세스 안에서 이 값이 실행 중에 바뀌면
# (예: 사이클 스레드는 예전 경로, UI 스레드는 새 경로를 보는 식으로) 원장/상태
# 파일이 두 곳으로 쪼개지는 사고가 난다. 값을 바꾸려면(테스트 등) 프로세스를
# 재시작하거나 os.environ 변경 후 이 모듈을 다시 import해야 한다.
DATA_ROOT = _resolve_data_root()

# ── 요구사항에 명시된 카테고리 ────────────────────────────────────────────
LOGS_DIR = DATA_ROOT / "logs"
STATE_DIR = DATA_ROOT / "state"
CACHE_DIR = DATA_ROOT / "cache"
PREDICTIONS_DIR = DATA_ROOT / "predictions"
ORDERS_DIR = DATA_ROOT / "orders"

EXECUTION_LEDGER_PATH = LOGS_DIR / "hynix_execution_ledger.csv"
SCHEDULER_HEARTBEAT_PATH = STATE_DIR / "scheduler_heartbeat.json"

# ── 그 외 이 프로젝트가 이미 쓰고 있던 data/ 하위 카테고리 — 전부 DATA_ROOT 기준으로
# 통일해야 재배포/재시작에도 캐시·모델·과거데이터·리포트가 유지된다.
RAW_DIR = DATA_ROOT / "raw"
FEATURES_DIR = DATA_ROOT / "features"
LABELS_DIR = DATA_ROOT / "labels"
CANDIDATES_DIR = DATA_ROOT / "candidates"
SELECTED_DIR = DATA_ROOT / "selected"
HISTORICAL_DIR = DATA_ROOT / "historical"
HYNIX_DIR = DATA_ROOT / "hynix"
MICRON_DIR = DATA_ROOT / "micron"
MODELS_DIR = DATA_ROOT / "models"
MODEL_CALIBRATION_DIR = DATA_ROOT / "model_calibration"
REPORTS_DIR = DATA_ROOT / "reports"
OUTPUT_DIR = DATA_ROOT / "output"
VOLUME_SPIKE_DIR = DATA_ROOT / "volume_spike"
ENHANCED_REPLAY_DIR = DATA_ROOT / "enhanced_replay"

_ALL_DIRS = (
    LOGS_DIR, STATE_DIR, CACHE_DIR, PREDICTIONS_DIR, ORDERS_DIR,
    RAW_DIR, FEATURES_DIR, LABELS_DIR, CANDIDATES_DIR, SELECTED_DIR,
    HISTORICAL_DIR, HYNIX_DIR, MICRON_DIR, MODELS_DIR, MODEL_CALIBRATION_DIR,
    REPORTS_DIR, OUTPUT_DIR, VOLUME_SPIKE_DIR, ENHANCED_REPLAY_DIR,
)


def ensure_data_dirs() -> None:
    """DATA_ROOT 및 모든 하위 카테고리 디렉토리를 생성한다(이미 있으면 아무 것도
    안 함). 앱 시작 시 한 번 호출한다."""
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def check_writable() -> dict:
    """DATA_ROOT에 실제로 파일을 쓰고 지워보는 쓰기 테스트.

    단순히 os.access()로 권한만 확인하지 않는다 — Render Persistent Disk가
    마운트는 됐지만 용량이 꽉 찼거나 권한이 잘못된 경우처럼, 권한 비트만으로는
    안 잡히는 실패도 실제 쓰기 시도로만 확인할 수 있다.
    """
    result = {"writable": False, "error": None, "data_root": str(DATA_ROOT), "checked_at": None}
    try:
        ensure_data_dirs()
        probe_path = DATA_ROOT / ".write_test"
        probe_path.write_text(str(time.time()), encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        result["writable"] = True
    except Exception as exc:
        result["error"] = str(exc)
    try:
        from app.utils.time_utils import kst_now

        result["checked_at"] = kst_now().isoformat(timespec="seconds")
    except Exception:
        pass
    return result


def file_info(path: Path) -> dict:
    """UI 표시용 — 파일 존재 여부/크기/마지막 수정시각(KST)."""
    info = {"path": str(path), "exists": False, "size_bytes": None, "modified_at": None}
    try:
        if not path.exists():
            return info
        stat = path.stat()
        info["exists"] = True
        info["size_bytes"] = stat.st_size
        info["modified_at"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    except Exception as exc:
        info["error"] = str(exc)
    return info


def data_path(*parts: str) -> Path:
    """DATA_ROOT 기준 임의 하위 경로. 위에 상수로 없는 일회성 경로에만 사용할 것 —
    자주 쓰는 카테고리는 위 상수(LOGS_DIR 등)를 직접 쓴다."""
    return DATA_ROOT.joinpath(*parts)
