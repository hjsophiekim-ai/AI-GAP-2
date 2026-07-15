"""
hynix_switch_state.py — 하이닉스⇄인버스 자동매매 상태 저장/복구.

mock과 real은 완전히 분리된 파일에 저장한다(`hynix_auto_state_mock.json`,
`hynix_auto_state_real.json`) — mock 거래가 real 화면에 섞이거나 반대로 섞이는
사고를 방지한다. 어느 파일을 볼지는 `active_mode` 포인터 파일로 판단하며,
UI에서 모드를 바꾸면 이 포인터도 함께 갱신된다. 손상/누락 시 예외를 던지지
않고 안전 기본값으로 복구하며, 저장은 임시파일 write 후 os.replace()로
원자적으로 수행한다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL
from app.utils.time_utils import kst_now

ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_DIR = ROOT / "data" / "state"

_DEFAULT_MOCK_BUDGET_KRW = 10_000_000.0

# mode별 상태 read-modify-write 전용 락. 백그라운드 3분 사이클 스레드, 1초 주기
# Dynamic Exit Watcher 스레드, Streamlit 요청 스레드(수동 실행 버튼)가 모두 같은
# mode의 state 파일을 동시에 load_state() → 필드 수정 → save_state_atomic() 할 수
# 있는데, 이 셋 사이에 락이 없으면 "lost update"가 발생한다 — 예: 스레드A가 읽은
# 뒤 스레드B가 realized_pnl_today_krw를 갱신·저장하고, 그 직후 스레드A가 (이미
# 낡은 값 기준으로 계산한) 자신의 결과를 저장하면서 스레드B의 변경분을 통째로
# 덮어쓴다(2026-07-10 실측 — 부분손절 1건의 손익이 이런 식으로 누락됨).
# 아래 with_state_lock()으로 "load_state ~ save_state_atomic" 전체를 감싸면 이
# read-modify-write 사이클 자체가 mode별로 직렬화되어 lost update가 사라진다.
_state_locks_guard = threading.Lock()
_state_locks: dict[str, threading.RLock] = {}


def _get_state_lock(mode: Optional[str]) -> threading.RLock:
    key = mode or "mock"
    with _state_locks_guard:
        if key not in _state_locks:
            _state_locks[key] = threading.RLock()
        return _state_locks[key]


_CROSS_PROCESS_LOCK_STALE_SECONDS = 30.0
_CROSS_PROCESS_LOCK_POLL_SECONDS = 0.05
_CROSS_PROCESS_LOCK_TIMEOUT_SECONDS = 20.0
_cross_process_lock_depth = threading.local()


def _cross_process_lock_path(mode: str) -> Path:
    return _STATE_DIR / f".hynix_auto_state_{mode}.lock"


@contextmanager
def _acquire_cross_process_lock(mode: str):
    """Real cross-process mutex for this mode's state file (atomic file creation).

    threading.RLock (_get_state_lock) only serializes within a single process.
    If two separate Python processes both run the 3-min cycle/Fast Watcher
    against the same data/state directory (2026-07-15 실측 — 여러 포트로 동시
    실행된 서버 프로세스 흔적: start_streamlit_8507.ps1/8512.cmd, 8503/8504/8506/
    8507 로그 파일들), the in-process RLock provides zero protection between
    them, and a lost-update race explains the reported symptoms (CSV shows
    1480 bought/648 sold but UI shows no holding; cycle completion timestamp
    earlier than start). This adds a genuine OS-level mutex via O_CREAT|O_EXCL.
    A lock older than _CROSS_PROCESS_LOCK_STALE_SECONDS is assumed to belong to
    a crashed process and is stolen rather than causing a permanent deadlock.
    Reentrant within the same thread (nested with_state_lock calls don't
    re-acquire the file lock).
    """
    depth = getattr(_cross_process_lock_depth, "depth", 0)
    if depth > 0:
        _cross_process_lock_depth.depth = depth + 1
        try:
            yield
        finally:
            _cross_process_lock_depth.depth = depth
        return

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _cross_process_lock_path(mode or "mock")
    deadline = time.monotonic() + _CROSS_PROCESS_LOCK_TIMEOUT_SECONDS
    acquired = False
    fd = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                acquired = True
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > _CROSS_PROCESS_LOCK_STALE_SECONDS:
                    try:
                        lock_path.unlink()
                        logger.warning(
                            "[HynixSwitchState] stale cross-process lock removed (mode=%s, age=%.1fs) "
                            "— owning process likely crashed without releasing it",
                            mode, age,
                        )
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    logger.error(
                        "[HynixSwitchState] cross-process lock timeout(mode=%s) after %.0fs — proceeding "
                        "without it to avoid a permanent deadlock; a concurrent write is possible this cycle",
                        mode, _CROSS_PROCESS_LOCK_TIMEOUT_SECONDS,
                    )
                    break
                time.sleep(_CROSS_PROCESS_LOCK_POLL_SECONDS)
        _cross_process_lock_depth.depth = 1
        yield
    finally:
        _cross_process_lock_depth.depth = 0
        if fd is not None:
            os.close(fd)
        if acquired:
            try:
                lock_path.unlink()
            except OSError:
                pass


@contextmanager
def with_state_lock(mode: Optional[str] = None):
    """이 mode의 state를 load→수정→save하는 전체 구간을 감싸는 컨텍스트 매니저.

    RLock(같은 프로세스 안)과 파일 기반 상호배제(여러 프로세스 사이)를 함께 건다 —
    RLock이므로 같은 스레드 안에서 중첩 호출해도 데드락이 나지 않는다.
    """
    lock = _get_state_lock(mode)
    with lock:
        with _acquire_cross_process_lock(mode or "mock"):
            yield


def _today_str() -> str:
    """KST(Asia/Seoul) 기준 오늘 날짜 — 서버가 UTC로 배포돼도 일일 상태(예산/거래
    횟수/실현손익) 리셋 경계가 KST 자정이 아니라 UTC 자정(=KST 09:00, 장중)에서
    발생하는 것을 방지한다."""
    return kst_now().strftime("%Y%m%d")


def _state_path(mode: str) -> Path:
    mode = mode if mode in ("mock", "real") else "mock"
    return _STATE_DIR / f"hynix_auto_state_{mode}.json"


def _active_mode_pointer_path() -> Path:
    return _STATE_DIR / "hynix_auto_state_active_mode.json"


def get_active_mode() -> str:
    """UI가 마지막으로 선택한 mode(mock/real). 포인터가 없으면 mock."""
    try:
        path = _active_mode_pointer_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("mode", "mock") if data.get("mode") in ("mock", "real") else "mock"
    except Exception as exc:
        logger.debug("[HynixSwitchState] active_mode 포인터 로드 실패: %s", exc)
    return "mock"


def set_active_mode(mode: str) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _active_mode_pointer_path().write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except Exception as exc:
        logger.debug("[HynixSwitchState] active_mode 포인터 저장 실패: %s", exc)


def _empty_position() -> dict:
    return {
        "symbol": None, "name": None, "quantity": 0, "avg_price": None, "entry_price": None,
        "entry_time": None, "partial_tp1_done": False, "partial_sl1_done": False,
        "highest_price": None, "lowest_price": None,
        "trailing_armed": False, "trailing_peak_price": None,
        "profit_lock_peak_pct": 0.0,
    }


def default_state(mode: str = "mock") -> dict:
    return {
        "date": _today_str(),
        "mode": mode,
        "position": _empty_position(),
        # 사용자 지정 평면(flat) 필드 — position 내용과 저장 시 동기화됨
        "current_position": None,
        "current_position_type": "NONE",
        "symbol": None,
        "name": None,
        "entry_price": None,
        "quantity": 0,
        "entry_time": None,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_trade_count": 0,
        "liquidation_done": False,
        # 예산/현금(mock 로컬 시뮬레이션 예산, real은 브로커 조회값으로 매 사이클 갱신)
        "mock_budget_krw": _DEFAULT_MOCK_BUDGET_KRW,
        "cash": _DEFAULT_MOCK_BUDGET_KRW if mode == "mock" else None,
        # 내부 운영 필드
        "trades_today": [],
        "realized_pnl_today_krw": 0.0,
        "gross_realized_pnl_today_krw": 0.0,
        "realized_pnl_today_pct": 0.0,
        "gross_realized_pnl_today_pct": 0.0,
        "daily_pnl_baseline_equity": None,
        "fired_windows": [],
        "liquidation_mode": False,
        "residual_position_error": False,
        "position_conflict": False,
        "critical_alert": None,
        "auto_trade_on": False,
        "weight_auto_apply_enabled": False,
        "daily_report_generated_date": None,
        "stopped": False,
        "stopped_reason": None,
        "last_order_cycle_bucket": None,
        "last_order_signature": None,
        "last_buy_price": None,
        "last_sell_price": None,
        "last_trade_time": None,
        "last_action": None,
        "last_order_id": None,
        "stop_loss_mode": "AUTO",  # AUTO | ALERT_ONLY | BATCH_MANUAL
        "last_stop_loss_signature": None,
        "pending_manual_stop_loss_alert": None,
    }


def _sync_flat_fields(state: dict) -> None:
    pos = state.get("position") or {}
    symbol = pos.get("symbol")
    qty = pos.get("quantity", 0) or 0
    if symbol == LONG_SYMBOL and qty > 0:
        position_type = "HYNIX"
    elif symbol == SHORT_SYMBOL and qty > 0:
        position_type = "INVERSE"
    else:
        position_type = "NONE"
        symbol = None

    state["current_position"] = symbol
    state["current_position_type"] = position_type
    state["symbol"] = symbol
    state["name"] = pos.get("name") if symbol else None
    state["entry_price"] = pos.get("entry_price") if symbol else None
    state["quantity"] = qty if symbol else 0
    state["entry_time"] = pos.get("entry_time") if symbol else None
    state["realized_pnl"] = state.get("realized_pnl_today_krw", 0.0)

    # "오늘 마지막 주문"(날짜 바뀌면 초기화)과 별개로 "전체 마지막 주문"(영구 보존)을
    # 항상 최신으로 미러링한다 — 오늘 값이 있을 때만 갱신하고, 없다고 지우지 않는다.
    if state.get("last_order_id"):
        state["all_time_last_order_id"] = state["last_order_id"]
        state["all_time_last_action"] = state.get("last_action")
        state["all_time_last_trade_time"] = state.get("last_trade_time")
    if state.get("last_buy_price"):
        state["all_time_last_buy_price"] = state["last_buy_price"]
    if state.get("last_sell_price"):
        state["all_time_last_sell_price"] = state["last_sell_price"]


def _iso_date(value) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y%m%d")
    except Exception:
        text = str(value)
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[:10].replace("-", "")
        return None


def _dict_state_date(value: dict, *timestamp_keys: str) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    raw_date = value.get("_state_date") or value.get("date")
    if raw_date:
        return str(raw_date).replace("-", "")[:8]
    for key in timestamp_keys:
        parsed = _iso_date(value.get(key))
        if parsed:
            return parsed
    return None


def _clear_intraday_decision_state(state: dict) -> None:
    for key in (
        "pending_entry",
        "trend_switch_confirm_tracker",
        "trend_switch_frequency_state",
        "last_trend_switch_plan",
        "fast_trend_watcher",
        "trend_switch_unconfirmed_order",
        "last_big_trend_result",
        "last_final_execution_decision",
        "last_pipeline_trace",
        "daily_return_calculation",
        "last_account_equity_snapshot",
        "dynamic_exit_last_decision",
        "last_cycle_ai_result",
    ):
        state[key] = None
    state["position_sync_error"] = None
    state["position_sync_status"] = None
    state["position_sync_block_new_orders"] = False


def _sanitize_intraday_state_dates(state: dict, today: str) -> None:
    """Drop stale day-scoped caches even when the top-level state date is current.

    A previous rollover could update `state["date"]` while leaving nested Enhanced
    switching/pullback/account snapshots from the prior session. Those fields must
    not influence today's order gate.
    """
    stale = False
    tracker_date = _dict_state_date(state.get("trend_switch_confirm_tracker"), "last_signal_at")
    freq_date = _dict_state_date(state.get("trend_switch_frequency_state"), "last_entry_at")
    pending_date = _dict_state_date(state.get("pending_entry"), "since")
    fast_date = _dict_state_date(state.get("fast_trend_watcher"), "last_checked_at", "updated_at", "last_signal_at", "as_of")
    account_date = _dict_state_date(state.get("last_account_equity_snapshot"), "as_of")
    daily_calc = state.get("daily_return_calculation") or {}
    daily_calc_date = _dict_state_date(daily_calc.get("account_snapshot") if isinstance(daily_calc, dict) else None, "as_of")
    big_trend = state.get("last_big_trend_result") or {}
    big_trend_date = None
    if isinstance(big_trend, dict):
        big_trend_date = _dict_state_date(big_trend.get("log_row"), "timestamp")

    for observed in (tracker_date, freq_date, pending_date, fast_date, account_date, daily_calc_date, big_trend_date):
        if observed and observed != today:
            stale = True
            break
    if stale:
        logger.warning("[HynixSwitchState] stale intraday Enhanced cache detected; clearing day-scoped fields")
        _clear_intraday_decision_state(state)


def _sanitize_position_sync_flags(state: dict) -> None:
    pos = state.get("position") or {}
    flat = not pos.get("symbol") or (pos.get("quantity") or 0) <= 0
    if state.get("position_sync_status") == "SYNCED":
        state["position_sync_block_new_orders"] = False
        if flat:
            state["position_sync_error"] = None
        return
    if flat and state.get("position_sync_block_new_orders") and not state.get("residual_position_error"):
        state["position_sync_block_new_orders"] = False
        state["position_sync_error"] = None
        state["position_sync_status"] = None


def load_state(mode: Optional[str] = None) -> dict:
    """상태 로드. mode를 지정하지 않으면 활성 모드(active_mode 포인터)를 사용.

    파일 없음/손상 시 예외 없이 안전 기본값 반환.
    """
    mode = mode or get_active_mode()
    path = _state_path(mode)
    try:
        if not path.exists():
            return default_state(mode)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state 파일이 dict 형식이 아님")

        defaults = default_state(mode)
        state = {**defaults, **raw}
        state["mode"] = mode
        state["position"] = {**_empty_position(), **(raw.get("position") or {})}

        today = _today_str()
        if state.get("date") != today:
            pos = state["position"]
            if pos.get("symbol") in (LONG_SYMBOL, SHORT_SYMBOL) and (pos.get("quantity") or 0) > 0:
                state["residual_position_error"] = True
                logger.error(
                    "[HynixSwitchState] 전일 포지션이 청산되지 않고 남아있음(프로그램 오류 의심, mode=%s): %s",
                    mode, pos,
                )
            state["date"] = today
            state["daily_trade_count"] = 0
            state["trades_today"] = []
            state["realized_pnl_today_krw"] = 0.0
            state["gross_realized_pnl_today_krw"] = 0.0
            state["realized_pnl_today_pct"] = 0.0
            state["gross_realized_pnl_today_pct"] = 0.0
            state["fired_windows"] = []
            state["liquidation_done"] = False
            state["liquidation_mode"] = False
            state["daily_pnl_baseline_equity"] = None
            state["last_order_cycle_bucket"] = None
            state["last_order_signature"] = None
            state["critical_alert"] = None
            # 전일 거래 잔재가 오늘 화면에 그대로 남아 "오늘 이미 거래됨"처럼 보이는
            # 것을 방지 — 날짜가 바뀌면 당일 거래 관련 필드를 전부 초기화한다.
            state["last_buy_price"] = None
            state["last_sell_price"] = None
            state["last_trade_time"] = None
            state["last_action"] = None
            state["last_order_id"] = None
            state["pending_entry"] = None
            state["trend_switch_confirm_tracker"] = None
            state["trend_switch_frequency_state"] = None
            state["last_trend_switch_plan"] = None
            state["fast_trend_watcher"] = None
            state["trend_switch_unconfirmed_order"] = None
            state["pending_manual_stop_loss_alert"] = None
            if mode == "mock":
                state["cash"] = state.get("mock_budget_krw", _DEFAULT_MOCK_BUDGET_KRW)

        # 15:15(강제청산 시각) 이전인데 liquidation_done=True로 남아있으면 항상 오류다
        # (테스트/E2E 스크립트가 실제 state를 직접 건드렸거나 다른 버그로 잘못 세팅된
        # 경우) — 날짜 롤오버 여부와 무관하게 매 로드마다 확인해 자동 복구한다.
        _sanitize_intraday_state_dates(state, today)
        _sanitize_position_sync_flags(state)

        if state.get("liquidation_done"):
            try:
                from app.trading.hynix_switch_risk_gate import should_liquidate_now

                if not should_liquidate_now(datetime.now()):
                    logger.error(
                        "[HynixSwitchState] 15:15 이전인데 liquidation_done=True로 기록되어 있어 "
                        "False로 자동 복구합니다(mode=%s) — 원인 조사 필요",
                        mode,
                    )
                    state["liquidation_done"] = False
                    state["critical_alert"] = (
                        "liquidation_done 이상 감지 및 자동 복구됨(15:15 이전에 True로 기록됨)"
                    )
            except Exception as exc:
                logger.debug("[HynixSwitchState] liquidation_done 시간 검증 실패(무해): %s", exc)

        _sync_flat_fields(state)
        return state
    except Exception as exc:
        logger.error("[HynixSwitchState] 상태 로드 실패(mode=%s) — 안전 기본값으로 복구: %s", mode, exc)
        return default_state(mode)


_SAVE_RETRY_ATTEMPTS = 8
_SAVE_RETRY_BASE_SLEEP_SEC = 0.03


def save_state_atomic(state: dict) -> bool:
    """상태를 원자적으로 저장(임시파일 write 후 os.replace). mode별 파일에 저장.

    Windows에서는 대상 파일이 다른 프로세스/스레드(예: Dynamic Exit 감시 스레드나
    다른 브라우저 탭의 세션이 동시에 load_state()로 읽는 중)에 의해 아주 짧게
    열려 있으면 os.replace가 WinError 5(Access Denied)로 실패할 수 있다. 이런
    경합은 보통 수십 ms 안에 풀리므로 지수적으로 늘어나는 짧은 대기와 함께
    재시도한다. 그래도 전부 실패하면(디스크/권한 문제 등 지속적 오류) 기존과
    동일하게 예외를 삼키고 로그만 남긴다 — 사이클 자체가 죽지는 않게 한다.

    Returns:
        True — 실제로 디스크에 반영됨. False — 재시도까지 모두 실패해 반영되지
        않음(UI의 "UI Synced" 표시가 이 값을 그대로 사용한다).
    """
    try:
        _sync_flat_fields(state)
        mode = state.get("mode", "mock")
        path = _state_path(mode)
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, default=str, indent=2), encoding="utf-8")

        last_exc: Optional[OSError] = None
        for attempt in range(_SAVE_RETRY_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                if attempt > 0:
                    logger.debug("[HynixSwitchState] 상태 저장 재시도 %d회 후 성공", attempt)
                return True
            except OSError as exc:
                last_exc = exc
                time.sleep(_SAVE_RETRY_BASE_SLEEP_SEC * (attempt + 1))
        logger.error("[HynixSwitchState] 상태 저장 실패(재시도 %d회 모두 실패): %s", _SAVE_RETRY_ATTEMPTS, last_exc)
        return False
    except Exception as exc:
        logger.error("[HynixSwitchState] 상태 저장 실패: %s", exc)
        return False


def reset_mock_state(budget_krw: Optional[float] = None) -> dict:
    """'mock 계좌 초기화' 버튼 — mock 상태를 완전히 새로 시작한다(포지션/거래횟수/현금 리셋)."""
    state = default_state("mock")
    if budget_krw is not None:
        state["mock_budget_krw"] = float(budget_krw)
        state["cash"] = float(budget_krw)
    save_state_atomic(state)

    try:
        from app.trading.dry_run_broker import _DATA_DIR

        dry_run_path = _DATA_DIR / f"{_today_str()}_dry_portfolio.json"
        if dry_run_path.exists():
            dry_run_path.unlink()
    except Exception as exc:
        logger.debug("[HynixSwitchState] DryRunBroker 포트폴리오 파일 삭제 실패(무해): %s", exc)

    return state
