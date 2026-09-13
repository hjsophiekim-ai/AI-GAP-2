"""프리마켓 carry SHADOW — 08:45~09:03 라이브 표본 **관측/기록 전용** (2026-09-13).

이 모듈이 절대 하지 않는 것
---------------------------
주문을 내지 않는다. ``order_executor`` 를 import 조차 하지 않는다. production
``RuntimeState`` 를 읽기만 하고 **쓰지 않는다**. 슬롯/exposure/원장/포지션을
전혀 소비하거나 변경하지 않는다. shadow 상태는 production trading state 와
**완전히 분리된 자체 JSON 파일**에 저장한다.

왜 필요한가
-----------
08:45~08:59 프리마켓 3분봉의 과거 커버리지가 **8.0%(70영업일 기준 28/350봉)** 에
불과해, 프리마켓 carry 전략을 백테스트로 판단할 수 없다(실진입 표본 3건).
그래서 실제 장에서 라이브 표본을 쌓는다. 판단은 표본이 모인 뒤에 한다.

기록 규칙 (사용자 사양 2026-09-13)
----------------------------------
    08:45~08:59 마지막 확정 플래그만 후보         PREMARKET_LAST_FLAG
    09:00 시점 방향 유지 여부                     OPEN_0900_STATE
    09:03 재확인(동일방향 + MACD gap confirmation) CARRY_CONFIRM_0903
    09:00~09:03 반대전환                          CARRY_CANCEL_REVERSE
    09:00 이후 정규장 플래그가 먼저                CARRY_CANCEL_REGULAR_FLAG
    승인                                          CARRY_ELIGIBLE
    가상 진입/청산                                CARRY_SHADOW_ENTRY / _EXIT
08:45 **이전** 플래그도 ``PREMARKET_FLAG`` 로 기록만 하고 후보로 삼지 않는다.
하루 1회만 후보를 승인한다. 날짜가 바뀌면 전부 리셋한다.

무결성
------
- CSV 는 append + 즉시 flush + fsync. 같은 (date, event_type, bar_time) 은
  중복 기록하지 않는다(프로세스 재시작 후에도 파일을 읽어 복원).
- 프로세스 내 락으로 동시 쓰기를 막는다.
- ``worker_instance_id`` 를 매 행에 남겨 재시작 여부를 추적한다.
- 모든 공개 함수는 **예외를 밖으로 던지지 않는다** — 이 모듈의 실패가 트레이딩
  틱을 절대 멈출 수 없다(fail-open).
"""
from __future__ import annotations

import csv
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

from app.logger import logger
from app.trading.macd2 import config
from app.trading.macd2 import early_take_profit
from app.trading.macd2 import position_sizing
from app.trading.macd2 import time_window_3slot
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2 import signal_engine
from app.trading.macd2 import time_window_position_manager as twpm
from app.trading.macd2.models import Direction
from app.utils.data_paths import PREMARKET_CARRY_DIR

KST = config.KST

# ── 이벤트 타입 ────────────────────────────────────────────────────────────
EV_PREMARKET_FLAG = "PREMARKET_FLAG"
EV_PREMARKET_LAST_FLAG = "PREMARKET_LAST_FLAG"
EV_OPEN_0900_STATE = "OPEN_0900_STATE"
EV_CARRY_CONFIRM_0903 = "CARRY_CONFIRM_0903"
EV_CARRY_CANCEL_REVERSE = "CARRY_CANCEL_REVERSE"
EV_CARRY_CANCEL_REGULAR_FLAG = "CARRY_CANCEL_REGULAR_FLAG"
EV_CARRY_ELIGIBLE = "CARRY_ELIGIBLE"
EV_CARRY_SHADOW_ENTRY = "CARRY_SHADOW_ENTRY"
EV_CARRY_SHADOW_EXIT = "CARRY_SHADOW_EXIT"

EVENT_COLUMNS = [
    "event_time", "bar_time", "recognition_time", "symbol", "macd", "signal",
    "gap", "prev_gap", "direction", "event_type", "entry_chop",
    "worker_instance_id", "strategy_mode",
]

SUMMARY_COLUMNS = [
    "date", "last_premarket_flag_time", "last_premarket_direction",
    "state_0900", "state_0903", "carry_confirmed", "cancel_reason",
    "theoretical_symbol", "theoretical_entry_time", "theoretical_entry_price",
    "theoretical_slot", "theoretical_chop", "theoretical_sizing",
    "theoretical_exit_time", "theoretical_exit_price", "theoretical_exit_reason",
    "theoretical_net_pct", "would_displace_existing_trade",
    "displaced_trade_time", "displaced_trade_net_pct",
    "worker_instance_id",
]

#: 이 컬럼 이름들은 GitHub 동기화 대상에 **절대** 들어가면 안 된다(방어용 목록은
#: github_analysis_sync 쪽에서도 한 번 더 검사한다).
FORBIDDEN_COLUMN_HINTS = (
    "account", "acct", "cano", "appkey", "app_key", "appsecret", "app_secret",
    "token", "secret", "password", "balance", "cash", "orderable", "nrcvb",
    "psbl", "deposit", "buying_power", "잔고", "계좌",
)

_LOCK = threading.RLock()
_WORKER_INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def worker_instance_id() -> str:
    return _WORKER_INSTANCE_ID


# ── 경로 ───────────────────────────────────────────────────────────────────
def events_path(day: str) -> Path:
    return PREMARKET_CARRY_DIR / f"premarket_carry_{day}.csv"


def summary_path() -> Path:
    return PREMARKET_CARRY_DIR / "premarket_carry_summary.csv"


def state_path() -> Path:
    """shadow 전용 상태 — production state 와 완전히 분리된 파일."""
    return PREMARKET_CARRY_DIR / "premarket_carry_shadow_state.json"


def _ensure_dir() -> None:
    PREMARKET_CARRY_DIR.mkdir(parents=True, exist_ok=True)


# ── CSV append (즉시 flush + fsync, 중복 방지) ─────────────────────────────
def _existing_keys(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return {(r.get("event_type", ""), r.get("bar_time", ""))
                    for r in csv.DictReader(fh)}
    except Exception:
        return set()


def append_event(row: dict, *, day: str) -> bool:
    """한 줄 append. 같은 (event_type, bar_time) 이 이미 있으면 **쓰지 않는다**.
    True = 실제로 기록함."""
    try:
        with _LOCK:
            _ensure_dir()
            p = events_path(day)
            key = (str(row.get("event_type", "")), str(row.get("bar_time", "")))
            if key in _existing_keys(p):
                return False
            new = not p.exists()
            with p.open("a", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({c: row.get(c, "") for c in EVENT_COLUMNS})
                fh.flush()
                os.fsync(fh.fileno())
            return True
    except Exception as exc:  # fail-open
        logger.warning(f"[PREMARKET_SHADOW] append_event 실패(무시): {exc}")
        return False


def upsert_summary(row: dict) -> bool:
    """날짜 1행 upsert. 기존 파일을 읽어 해당 date 를 갈아끼우고 원자적으로 쓴다."""
    try:
        with _LOCK:
            _ensure_dir()
            p = summary_path()
            rows: list[dict] = []
            if p.exists():
                with p.open("r", encoding="utf-8-sig", newline="") as fh:
                    rows = [r for r in csv.DictReader(fh)]
            rows = [r for r in rows if r.get("date") != str(row.get("date"))]
            rows.append({c: row.get(c, "") for c in SUMMARY_COLUMNS})
            rows.sort(key=lambda r: str(r.get("date", "")))
            tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
            with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c, "") for c in SUMMARY_COLUMNS})
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
            return True
    except Exception as exc:
        logger.warning(f"[PREMARKET_SHADOW] upsert_summary 실패(무시): {exc}")
        return False


# ── shadow 상태 (production state 와 분리) ────────────────────────────────
@dataclass
class ShadowState:
    date: str = ""
    last_flag_time: Optional[str] = None
    last_flag_direction: Optional[str] = None
    state_0900: str = ""
    state_0903: str = ""
    carry_confirmed: bool = False
    cancel_reason: str = ""
    used_today: bool = False
    # 가상 포지션
    pos_symbol: Optional[str] = None
    pos_entry_time: Optional[str] = None
    pos_entry_price: Optional[float] = None
    pos_slot: Optional[int] = None
    pos_chop: bool = False
    pos_sizing: Optional[float] = None
    pos_tp1_done: bool = False
    pos_peak: float = 0.0
    pos_etp_peak: float = 0.0
    pos_realized: float = 0.0
    pos_qty_frac: float = 1.0
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_pct: Optional[float] = None
    worker_instance_id: str = ""


def load_shadow_state() -> ShadowState:
    try:
        p = state_path()
        if p.exists():
            return ShadowState(**{k: v for k, v in
                                  json.loads(p.read_text(encoding="utf-8")).items()
                                  if k in ShadowState.__dataclass_fields__})
    except Exception as exc:
        logger.warning(f"[PREMARKET_SHADOW] state 복원 실패, 초기화: {exc}")
    return ShadowState()


def save_shadow_state(st: ShadowState) -> None:
    try:
        with _LOCK:
            _ensure_dir()
            p = state_path()
            tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(asdict(st), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, p)
    except Exception as exc:
        logger.warning(f"[PREMARKET_SHADOW] state 저장 실패(무시): {exc}")


# ── 판정 헬퍼 (순수) ──────────────────────────────────────────────────────
def is_premarket_carry_window(t: dtime) -> bool:
    """08:45:00 ~ 08:59:59 — 후보로 삼을 수 있는 구간."""
    return config.PREMARKET_CARRY_WINDOW_START <= t < config.SESSION_OPEN


def _row(*, now: datetime, bar_dt: Optional[datetime], event_type: str,
         direction: Optional[str] = None, macd_snap=None,
         recognition: Optional[datetime] = None, entry_chop: Optional[bool] = None,
         symbol: str = "", mode: str = "") -> dict:
    def g(name):
        # MacdSnapshot 의 실제 필드명 매핑 — gap = current_diff(macd-signal),
        # prev_gap = previous_diff. 이 모듈은 스냅샷을 **읽기만** 한다.
        alias = {"gap": "current_diff", "prev_gap": "previous_diff"}
        v = getattr(macd_snap, alias.get(name, name), None) if macd_snap is not None else None
        return "" if v is None else round(float(v), 6)
    return {
        "event_time": now.astimezone(KST).isoformat(),
        "bar_time": ("" if bar_dt is None else bar_dt.astimezone(KST).isoformat()),
        "recognition_time": ("" if recognition is None
                             else recognition.astimezone(KST).isoformat()),
        "symbol": symbol,
        "macd": g("macd"), "signal": g("signal"),
        "gap": g("gap"), "prev_gap": g("prev_gap"),
        "direction": direction or "",
        "event_type": event_type,
        "entry_chop": ("" if entry_chop is None else int(bool(entry_chop))),
        "worker_instance_id": _WORKER_INSTANCE_ID,
        "strategy_mode": mode,
    }


def _net_pct(entry_price: float, price: float, direction: str) -> float:
    """가상 손익 — 방향 무관하게 ETF 가격 기준(진입 종목이 이미 방향을 반영)."""
    if not entry_price:
        return 0.0
    return (float(price) - float(entry_price)) / float(entry_price) * 100.0


# ── 메인 관측 훅 ──────────────────────────────────────────────────────────
def observe(*, state, macd_snap, bars_3m, now: datetime,
            quotes: Optional[dict] = None) -> Optional[dict]:
    """매 틱 1회 호출. **절대 예외를 밖으로 던지지 않는다.**

    ``state`` 는 production RuntimeState 이지만 **읽기 전용**으로만 쓴다
    (전략 모드 판별과 displaced 판정용). 어떤 필드도 쓰지 않는다.
    """
    try:
        return _observe_inner(state=state, macd_snap=macd_snap, bars_3m=bars_3m,
                              now=now, quotes=quotes or {})
    except Exception as exc:
        logger.warning(f"[PREMARKET_SHADOW] observe 실패(무시): {exc}")
        return None


def _observe_inner(*, state, macd_snap, bars_3m, now: datetime, quotes: dict):
    if macd_snap is None or getattr(macd_snap, "bar_dt", None) is None:
        return None
    now_k = now.astimezone(KST)
    day = now_k.strftime("%Y%m%d")
    mode = time_window_3slot.active_3slot_mode(state) or ""

    st = load_shadow_state()
    if st.date != day:                       # 날짜 rollover — 전부 초기화
        st = ShadowState(date=day, worker_instance_id=_WORKER_INSTANCE_ID)
        save_shadow_state(st)

    bar_dt = macd_snap.bar_dt.astimezone(KST)
    bar_t = bar_dt.time()
    # production 과 **같은 순수 함수**로 확정 플래그 방향을 얻는다.
    flag_dir = signal_engine.confirmed_macd_flag_condition(macd_snap)
    dir_v = (flag_dir.value if flag_dir in (Direction.UP_RED, Direction.DOWN_BLUE)
             else None)
    is_flag = dir_v is not None

    # ── 1) 프리마켓 플래그 기록 ──────────────────────────────────────────
    if bar_t < config.SESSION_OPEN and is_flag and dir_v:
        append_event(_row(now=now, bar_dt=bar_dt, event_type=EV_PREMARKET_FLAG,
                          direction=dir_v, macd_snap=macd_snap, mode=mode), day=day)
        if is_premarket_carry_window(bar_t) and not st.used_today:
            # 규칙 1: **마지막** 것만 후보 (뒤에 오면 덮어쓴다)
            st.last_flag_time = bar_dt.isoformat()
            st.last_flag_direction = dir_v
            st.cancel_reason = ""
            save_shadow_state(st)
            append_event(_row(now=now, bar_dt=bar_dt, event_type=EV_PREMARKET_LAST_FLAG,
                              direction=dir_v, macd_snap=macd_snap, mode=mode), day=day)
        return {"stage": "PREMARKET_FLAG", "direction": dir_v}

    # ── 2) 09:00 상태 ────────────────────────────────────────────────────
    if (st.last_flag_direction and not st.state_0900
            and bar_t >= config.SESSION_OPEN):
        same = (dir_v == st.last_flag_direction) if dir_v else True
        st.state_0900 = "SAME_DIRECTION" if same else "REVERSED"
        if not same:
            st.cancel_reason = EV_CARRY_CANCEL_REVERSE
            st.state_0903 = "CANCELLED"
            append_event(_row(now=now, bar_dt=bar_dt, event_type=EV_CARRY_CANCEL_REVERSE,
                              direction=dir_v, macd_snap=macd_snap, mode=mode), day=day)
        save_shadow_state(st)
        append_event(_row(now=now, bar_dt=bar_dt, event_type=EV_OPEN_0900_STATE,
                          direction=st.last_flag_direction, macd_snap=macd_snap,
                          mode=mode), day=day)

    # ── 3) 09:00 이후 정규장 플래그가 먼저 나오면 취소 ───────────────────
    if (st.last_flag_direction and not st.carry_confirmed and not st.state_0903
            and bar_t >= config.SESSION_OPEN and is_flag and dir_v
            and bar_dt.isoformat() != st.last_flag_time):
        st.cancel_reason = EV_CARRY_CANCEL_REGULAR_FLAG
        st.state_0903 = "CANCELLED"
        save_shadow_state(st)
        append_event(_row(now=now, bar_dt=bar_dt,
                          event_type=EV_CARRY_CANCEL_REGULAR_FLAG,
                          direction=dir_v, macd_snap=macd_snap, mode=mode), day=day)
        upsert_summary(_summary_row(st))
        return {"stage": "CANCELLED", "reason": EV_CARRY_CANCEL_REGULAR_FLAG}

    # ── 4) 09:03 재확인 ──────────────────────────────────────────────────
    if (st.last_flag_direction and st.state_0900 == "SAME_DIRECTION"
            and not st.state_0903 and not st.used_today
            and bar_t >= config.SESSION_OPEN and bars_3m is not None):
        recognition = bar_dt.replace(second=0, microsecond=0)
        flag_dt = datetime.fromisoformat(st.last_flag_time)
        d = Direction(st.last_flag_direction)
        dec = twf.evaluate_time_window_entry(
            bars_3m, d, flag_dt, recognition,
            position_direction=None, morning_entry_count=0,
            afternoon_entry_count=0, daily_entry_count=0)
        ok = bool(getattr(dec, "approved", False))
        st.state_0903 = "CONFIRMED" if ok else "REJECTED"
        st.used_today = True
        if not ok:
            st.cancel_reason = str(getattr(dec, "block_reason", "") or "REJECTED")
        append_event(_row(now=now, bar_dt=bar_dt, recognition=recognition,
                          event_type=EV_CARRY_CONFIRM_0903,
                          direction=st.last_flag_direction, macd_snap=macd_snap,
                          mode=mode), day=day)
        if ok:
            chop = early_take_profit.evaluate_entry_chop(bars_3m, d, recognition)
            st.carry_confirmed = True
            st.pos_chop = bool(chop.is_chop)
            st.pos_sizing = float(position_sizing.evaluate(
                state, entry_chop=st.pos_chop).applied)
            st.pos_slot = 1
            st.pos_symbol = _target_symbol(d)
            px = quotes.get(st.pos_symbol)
            st.pos_entry_time = recognition.isoformat()
            st.pos_entry_price = (float(px) if px else None)
            append_event(_row(now=now, bar_dt=bar_dt, recognition=recognition,
                              event_type=EV_CARRY_ELIGIBLE,
                              direction=st.last_flag_direction, macd_snap=macd_snap,
                              entry_chop=st.pos_chop, symbol=st.pos_symbol,
                              mode=mode), day=day)
            if st.pos_entry_price:
                append_event(_row(now=now, bar_dt=bar_dt, recognition=recognition,
                                  event_type=EV_CARRY_SHADOW_ENTRY,
                                  direction=st.last_flag_direction, macd_snap=macd_snap,
                                  entry_chop=st.pos_chop, symbol=st.pos_symbol,
                                  mode=mode), day=day)
        save_shadow_state(st)
        upsert_summary(_summary_row(st))
        return {"stage": st.state_0903}

    # ── 5) 가상 포지션 추적 (X2-lite 청산 규칙 그대로) ──────────────────
    if st.carry_confirmed and st.pos_entry_price and not st.exit_reason:
        _advance_shadow_position(st, now=now, bar_dt=bar_dt, quotes=quotes,
                                 day=day, macd_snap=macd_snap, mode=mode)
    return {"stage": "TRACKING" if st.carry_confirmed else "IDLE"}


def _target_symbol(d: Direction) -> str:
    return config.LONG_SYMBOL if d == Direction.UP_RED else config.INVERSE_SYMBOL


def _advance_shadow_position(st: ShadowState, *, now: datetime, bar_dt: datetime,
                             quotes: dict, day: str, macd_snap, mode: str) -> None:
    """X2-lite override 로 래더를 돌린다 — **순수 함수만** 쓰고 주문은 없다."""
    px = quotes.get(st.pos_symbol)
    if not px:
        return
    net = _net_pct(st.pos_entry_price, px, st.last_flag_direction or "")
    st.pos_peak = max(float(st.pos_peak or 0.0), net)
    st.pos_etp_peak = max(float(st.pos_etp_peak or 0.0), net)

    mode_key = time_window_3slot.MODE_X2LITE_3SLOT
    ov = time_window_3slot.exit_overrides(mode_key)
    tp2 = time_window_3slot.morning_tp2_pct_override(mode_key)
    reason = None

    if now.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
        reason = config.EXIT_FORCED_LIQUIDATION
    else:
        d = twpm.evaluate_position(
            session="MORNING", net_return_pct=net,
            tp1_done=bool(st.pos_tp1_done), peak_net_return=float(st.pos_peak),
            tp2_pct_override=tp2, **ov)
        if d.exit_reason == config.EXIT_TW_TP1_PARTIAL:
            st.pos_realized += st.pos_qty_frac * d.sell_fraction * net
            st.pos_qty_frac *= (1.0 - d.sell_fraction)
            st.pos_tp1_done = True
            save_shadow_state(st)
            return
        if d.exit_reason is not None:
            reason = d.exit_reason
        else:
            trig, flr = (config.X2LITE_EARLY_TP_TRIGGER_PCT,
                         config.X2LITE_EARLY_TP_FLOOR_PCT)
            e = early_take_profit.evaluate(
                entry_chop=bool(st.pos_chop),
                peak_net_return_pct=float(st.pos_etp_peak),
                net_return_pct=net, trigger_pct=trig, floor_pct=flr)
            if e.exit_reason is not None:
                reason = e.exit_reason

    if reason is None:
        save_shadow_state(st)
        return
    st.exit_time = now.astimezone(KST).isoformat()
    st.exit_price = float(px)
    st.exit_reason = reason
    st.net_pct = round(st.pos_realized + st.pos_qty_frac * net, 6)
    save_shadow_state(st)
    append_event(_row(now=now, bar_dt=bar_dt, event_type=EV_CARRY_SHADOW_EXIT,
                      direction=st.last_flag_direction, macd_snap=macd_snap,
                      entry_chop=st.pos_chop, symbol=st.pos_symbol, mode=mode), day=day)
    upsert_summary(_summary_row(st))


def _summary_row(st: ShadowState) -> dict:
    return {
        "date": st.date,
        "last_premarket_flag_time": st.last_flag_time or "",
        "last_premarket_direction": st.last_flag_direction or "",
        "state_0900": st.state_0900, "state_0903": st.state_0903,
        "carry_confirmed": int(bool(st.carry_confirmed)),
        "cancel_reason": st.cancel_reason,
        "theoretical_symbol": st.pos_symbol or "",
        "theoretical_entry_time": st.pos_entry_time or "",
        "theoretical_entry_price": ("" if st.pos_entry_price is None else st.pos_entry_price),
        "theoretical_slot": ("" if st.pos_slot is None else st.pos_slot),
        "theoretical_chop": int(bool(st.pos_chop)),
        "theoretical_sizing": ("" if st.pos_sizing is None else st.pos_sizing),
        "theoretical_exit_time": st.exit_time or "",
        "theoretical_exit_price": ("" if st.exit_price is None else st.exit_price),
        "theoretical_exit_reason": st.exit_reason or "",
        "theoretical_net_pct": ("" if st.net_pct is None else st.net_pct),
        "would_displace_existing_trade": "",
        "displaced_trade_time": "",
        "displaced_trade_net_pct": "",
        "worker_instance_id": st.worker_instance_id or _WORKER_INSTANCE_ID,
    }


# ── UI 조회용 (읽기 전용) ─────────────────────────────────────────────────
def read_summary(limit: int = 200) -> list[dict]:
    try:
        p = summary_path()
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.DictReader(fh)]
        return rows[-limit:]
    except Exception:
        return []


def today_view(now: Optional[datetime] = None) -> dict:
    """UI 패널용 요약 — 절대 상태를 바꾸지 않는다."""
    try:
        st = load_shadow_state()
        rows = read_summary()
        done = [r for r in rows if str(r.get("theoretical_net_pct", "")) not in ("", "None")]
        recent = done[-20:]
        nets = [float(r["theoretical_net_pct"]) for r in recent]
        return {
            "date": st.date,
            "last_flag_time": st.last_flag_time,
            "last_flag_direction": st.last_flag_direction,
            "state_0900": st.state_0900,
            "state_0903": st.state_0903,
            "carry_confirmed": bool(st.carry_confirmed),
            "cancel_reason": st.cancel_reason,
            "shadow_entry_price": st.pos_entry_price,
            "shadow_sizing": st.pos_sizing,
            "shadow_net_pct": st.net_pct,
            "shadow_exit_reason": st.exit_reason,
            "total_samples": len(done),
            "recent20_n": len(recent),
            "recent20_win_rate": (100.0 * sum(1 for x in nets if x > 0) / len(nets)
                                  if nets else None),
            "recent20_avg_net": (sum(nets) / len(nets) if nets else None),
        }
    except Exception:
        return {"date": "", "total_samples": 0, "recent20_n": 0}
