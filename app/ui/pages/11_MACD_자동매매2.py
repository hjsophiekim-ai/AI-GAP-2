"""
11_MACD_자동매매2.py — ReadOnly UI for MACD2 (독립 신규 모듈)

MACD2는 app/trading/macd2/* 로 완전히 독립되어 있으며, 다른 자동매매
엔진 코드를 호출하지 않는다. UI는 command 기록(시작/중지)과 service.get_snapshot()
읽기만 수행한다 — MACD 계산·network 호출·Worker 생성/reload를 UI에서
직접 하지 않는다(docs/MACD2_LOGIC.md §16).

2026-08-18 사용자 요청: 화면이 너무 지저분해서 MU_MACD 페이지(12_MU_MACD_
자동매매.py)와 동일한 수준으로 간결하게 재구성 — 필터는 "시간대별 최적거래
필터"와 "퀵 Profit 익절"만 남기고, 그 외 필터(강한 플래그/추세전환장/Trend
Persistence/2% 3회진입/Profit Lock)의 토글과 진단 패널, 그리고 깊은 디버그용
진단 패널(운영 진단/데이터 저장 경로/주문 sizing/QUOTE_STALE/1분봉 history/
Provisional forming-bar/Signed-B shadow/필터 탈락 신호 등)은 화면에서 뺐다.
숨긴 필터들의 백엔드 로직/상태 필드/service 메서드는 전혀 건드리지 않았다 —
그 필터가 이미 state 파일에 ON으로 저장되어 있다면 그 설정 그대로 계속
동작한다(단, UI로는 더 이상 켜고 끌 수 없다 — 필요하면 .env로 제어).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from datetime import datetime, time as dtime
from html import escape

import pandas as pd
import streamlit as st

from app.ui import macd2_summary
from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.macd2 import config as macd2_config
from app.trading.macd2 import time_window_3slot as macd2_time_window_3slot  # noqa: E402
from app.trading.macd2 import early_take_profit  # noqa: E402
from app.trading.macd2 import position_sizing as macd2_position_sizing  # noqa: E402
from app.trading.macd2 import ledger  # noqa: E402
from app.trading.macd2 import worker as macd2_worker  # noqa: E402
from app.trading.macd2.service import get_service  # noqa: E402

# Display-only threshold, not an order-blocking rule (that remains
# macd2_config.FORCE_LIQUIDATE_AT/NEW_ENTRY_CUTOFF, untouched) — used only to
# tell "장 마감 후 대기" apart from "장전 대기" when bootstrap has no today
# bars yet (docs §21 2026-07-24 bootstrap-diagnostics UI addition).
_MARKET_CLOSE_HINT = dtime(15, 30)


def _worker_status(state, worker_stats: dict) -> str:
    """STOPPED (정상, auto_trade_on=False) / STARTING (스레드 있음, 아직 tick
    없음) / RUNNING (<=10s) / DELAYED (10~15s) / STALLED (>15s) / DEAD
    (auto_trade_on=True인데 Worker 스레드/객체 자체가 없음 — 프로세스 재시작
    후 복구되지 않은 상태)."""
    if not state.auto_trade_on:
        return "STOPPED"
    if not worker_stats:
        return "DEAD"
    age = worker_stats.get("last_tick_age_sec")
    if age is None:
        return "STARTING"
    if age <= 10:
        return "RUNNING"
    if age <= 15:
        return "DELAYED"
    return "STALLED"


def _quote_status(quotes: dict) -> str:
    # 2026-08-24: mirrors market_data.MarketDataService.quote_status()'s
    # symbol set -- WATCH_SYMBOL(000660) is signal-source-only and never
    # gates an order, so it's excluded here too (else this fallback path
    # would disagree with the service's own badge value).
    for symbol in (macd2_config.LONG_SYMBOL, macd2_config.INVERSE_SYMBOL):
        snap = quotes.get(symbol)
        if snap is None or snap.error or not snap.price:
            return "PARTIAL_ERROR"
        if snap.age_sec is not None and snap.age_sec > macd2_config.QUOTE_MAX_AGE_SEC:
            return "PARTIAL_STALE"
    return "READY"


def _bootstrap_status(state, bootstrap_last_result: dict | None) -> str:
    if state.warmup_ready:
        return "OK"
    reason = str((bootstrap_last_result or {}).get("reason") or state.order_block_reason or "")
    if "NO_1M_BARS" in reason or "TODAY_ONLY_WARMING_UP" in reason:
        now_t = datetime.now(macd2_config.KST).time()
        if now_t < macd2_config.SESSION_OPEN:
            return "PREMARKET_WAIT"
        if now_t >= _MARKET_CLOSE_HINT:
            return "MARKET_CLOSED_WAIT"
    return "FAILED" if bootstrap_last_result is not None or state.order_block_reason else "PENDING"


def _parse_flag_event_time(row: dict) -> datetime | None:
    completed_bar_at = str(row.get("completed_bar_at") or "")
    trading_date = str(row.get("trading_date") or "")
    if len(completed_bar_at) == 6 and completed_bar_at.isdigit() and len(trading_date) == 8 and trading_date.isdigit():
        try:
            return datetime.strptime(f"{trading_date}{completed_bar_at}", "%Y%m%d%H%M%S").replace(tzinfo=macd2_config.KST)
        except ValueError:
            pass
    for key in ("bar_start_at", "bar_end_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw)).astimezone(macd2_config.KST)
        except ValueError:
            pass
    signal_id = str(row.get("signal_id") or "")
    parts = signal_id.split("_")
    if len(parts) >= 2 and len(parts[1]) == 6 and parts[1].isdigit():
        if len(trading_date) == 8 and trading_date.isdigit():
            try:
                return datetime.strptime(f"{trading_date}{parts[1]}", "%Y%m%d%H%M%S").replace(tzinfo=macd2_config.KST)
            except ValueError:
                return None
    return None


#: "마지막 FLAG EVENT" 에 절대 들어가면 안 되는 signal_type — 이들은 MACD 가
#: 탐지한 플래그가 아니라 **주문 의도** 행이고, direction 도 탐지된 방향이
#: 아니라 예약/사용자가 지정한 방향이다. 2026-09-16 실거래 사고에서 09:03
#: 예약매수(DOWN_BLUE) 행이 마지막 FLAG EVENT 로 표시되어, 같은 시각에 실제로
#: 확정된 RED 플래그를 가려버렸다.
_NON_FLAG_SIGNAL_TYPES = {
    "SCHEDULED_ENTRY_0903",
    "PREMARKET_CARRY_TW",
    "MANUAL_ENTRY",
    "MANUAL_LIQUIDATION",
}


def _is_flag_event_row(row: dict) -> bool:
    return str(row.get("signal_type") or "") not in _NON_FLAG_SIGNAL_TYPES


def _latest_flag_event(rows: list[dict]) -> dict | None:
    # 주문 의도 행은 제외한다. 전부 제외돼 남는 게 없으면 None 을 돌려주고,
    # 호출부는 state.latest_primary_flag(= 마지막으로 실제 탐지된 플래그)로
    # 자연히 되돌아간다.
    rows = [row for row in rows if _is_flag_event_row(row)]
    if not rows:
        return None
    timed_rows = [(_parse_flag_event_time(row), row) for row in rows]
    timed_rows = [(ts, row) for ts, row in timed_rows if ts is not None]
    if not timed_rows:
        return rows[-1] if rows else None
    return max(timed_rows, key=lambda item: item[0])[1]


def _hhmmss(raw: str) -> str:
    raw = str(raw or "")
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[0:2]}:{raw[2:4]}:{raw[4:6]}"
    try:
        return datetime.fromisoformat(raw).astimezone(macd2_config.KST).strftime("%H:%M:%S")
    except ValueError:
        return raw


def _signal_flag_time(row: dict) -> str:
    if row.get("signal_bar_at"):
        return _hhmmss(row.get("signal_bar_at"))
    return _hhmmss(row.get("completed_bar_at"))


def _signal_confirm_time(row: dict) -> str:
    return _hhmmss(row.get("signal_confirmed_at") or row.get("detected_at"))


def _is_display_signal(row: dict) -> bool:
    signal_id = str(row.get("signal_id") or "")
    if signal_id.startswith("RECONCILE_DISCOVERED"):
        return False
    signal_type = str(row.get("signal_type") or "")
    return signal_type in {
        "INITIAL",
        "REVERSAL",
        "PREMARKET_CARRY_TW",
        "SCHEDULED_ENTRY_0903",
        "MANUAL_ENTRY",
        "MANUAL_LIQUIDATION",
        # 2026-09-02 사용자 요청: T+3 재확인 결과가 "신규 진입"(보유 포지션이
        # 없거나 같은 방향이라 반대매매가 아닌 경우)으로 거절된 행은
        # worker.py가 signal_type="TIME_WINDOW_CONFIRM"(TW2)/
        # "TW2_3SLOT_CONFIRM"(TW2 3-SLOT)으로 기록하는데, 이 목록에 없어서
        # 화면에서 통째로 숨겨져 있었다 -- 그 결과 사용자는 최초 등록 행
        # ("FILTERED_OUT / TIME_WINDOW_PENDING_CONFIRMATION", 아직 판정 전
        # 이라는 뜻일 뿐 거절 사유가 아님)만 보고, 실제 거절 사유(품질점수
        # 부족/TEG 거절/최대진입횟수 초과 등, block_reason 컬럼에는 이미
        # 기록되어 있었음)는 볼 방법이 없었다. 반대매매(REVERSAL) 거절은
        # 이미 정상적으로 이 목록에 있는 "REVERSAL" 경로로 보이고 있었으므로
        # (_execute_reversal_exit_only_for_filtered_entry), 이번 추가로
        # 신규진입 거절도 동일하게 사유가 보이게 된다. 휩쏘 HOLD 행도 이
        # 두 signal_type으로 기록되므로 함께 노출된다.
        "TIME_WINDOW_CONFIRM",
        "TW2_3SLOT_CONFIRM",
    }


def _signal_label(row: dict) -> str:
    signal_type = str(row.get("signal_type") or "")
    signal_id = str(row.get("signal_id") or "")
    if signal_type == "PREMARKET_CARRY_TW":
        return "프리마켓 승계"
    if signal_type == "SCHEDULED_ENTRY_0903":
        return "09:03 예약"
    if signal_type == "MANUAL_ENTRY":
        return "수동 진입"
    if signal_type == "MANUAL_LIQUIDATION":
        return "수동 청산"
    if signal_type in ("TIME_WINDOW_CONFIRM", "TW2_3SLOT_CONFIRM"):
        # T+3 재확인 결과 행 -- 최초 등록("플래그"/"반대 플래그") 행과 구분되게
        # 표시해 "같은 걸 두 번 보여주나" 하는 혼동을 줄인다.
        return "재확인(T+3)"
    # 2026-09-03 real incident: worker.py는 T+3 재확인이 "승인"된 경우
    # signal_type을 (거절된 경우와 달리) "TW2_3SLOT_CONFIRM"/
    # "TIME_WINDOW_CONFIRM"으로 남기지 않고 다른 진입/전환 경로와 동일하게
    # "INITIAL"/"REVERSAL"로 남긴다(_resolve_tw2_3slot_candidate_body의
    # signal_type = "REVERSAL" if position else "INITIAL") -- signal_id
    # 자체는 항상 ":TW2_3SLOT_CONFIRM"/":TW_CONFIRM" 접미사를 유지하므로,
    # 승인 여부와 무관하게 이 접미사만으로 "이건 새 플래그가 아니라 앞선
    # pending 후보의 T+3 확정 체결/전환 행"임을 판별한다. 이게 없으면
    # 승인된 진입이 마치 근거 없이 갑자기 나타난 새 플래그처럼 보인다(실제
    # 사용자 혼동 사례: 13:09 플래그의 정상 T+3 승인·체결이 화면엔 그냥
    # "13:12 플래그 UP_RED"로 보여 "오류인가?" 오인).
    if signal_id.endswith(":TW2_3SLOT_CONFIRM") or signal_id.endswith(":TW_CONFIRM"):
        return "재확인(T+3) 승인"
    return "반대 플래그" if signal_type == "REVERSAL" else "플래그"


_ORDER_RESULT_DISPLAY_LABELS = {
    # 2026-09-02 사용자 요청: 반대 플래그가 확정됐지만(T+3 재확인 결과 휩쏘로
    # 판정되어) 기존 포지션을 청산하지 않고 그대로 보유를 유지한 경우 -- TW2/
    # TW2 3-SLOT 둘 다 worker.py가 동일한 리터럴 "TIME_WINDOW_WHIPSAW_HOLD"를
    # order_result에 기록한다(worker.py의 order_result_override 처리, TW2/
    # TW2_3SLOT 분기 공통) -- 신호 원장에서 원문 그대로 보여주는 대신 한글로
    # 표시한다. 원장 저장값 자체는 변경하지 않음(표시만 매핑).
    "TIME_WINDOW_WHIPSAW_HOLD": "휩쏘보류",
}


def _order_summary(row: dict) -> str:
    result = str(row.get("order_result") or row.get("final_result") or "NO_ORDER")
    result = _ORDER_RESULT_DISPLAY_LABELS.get(result, result)
    reason = str(row.get("block_reason") or row.get("failure_stage") or "")
    broker_order_id = str(row.get("broker_order_id") or "")
    order_type = str(row.get("order_type") or "")
    price = row.get("order_price") or ""
    qty = row.get("filled_qty") or row.get("final_qty") or row.get("requested_qty") or ""
    pieces = [result]
    if broker_order_id:
        pieces.append(f"주문번호 {broker_order_id}")
    if qty:
        pieces.append(f"{qty}주")
    if price:
        pieces.append(f"@ {price}")
    if order_type:
        pieces.append(order_type)
    if reason:
        pieces.append(reason)
    return " / ".join(str(p) for p in pieces if str(p))


def _signal_timeline_rows(rows: list[dict]) -> list[dict]:
    timeline: list[dict] = []
    for row in rows:
        if not _is_display_signal(row):
            continue
        direction = str(row.get("direction") or row.get("confirmed_direction") or "")
        label = _signal_label(row)
        timeline.append({
            "구분": "플래그/확정",
            "시각": f"{_signal_flag_time(row)} -> {_signal_confirm_time(row)}",
            "내용": f"{label} {direction}".strip(),
        })
        timeline.append({
            "구분": "주문",
            "시각": _hhmmss(row.get("order_requested_at")) or "-",
            "내용": _order_summary(row),
        })
    return timeline


# 2026-08-31 사용자 요청: 신호 원장/체결 원장이 각각 컬럼이 너무 많고 시각도
# 원인도 한눈에 안 들어와 — 매수/매도 각 체결(레그) 하나당 딱 한 행으로,
# "언제/무슨 종목/몇 주/얼마/왜(레드-블루 또는 손절-익절 등)/순이익/수수료"만
# 보여주는 표를 별도로 추가한다. 기존 신호 원장/체결 원장(원본 컬럼 포함)은
# 진단용으로 그대로 남긴다 — 삭제하지 않는다.
_SYMBOL_DISPLAY_LABELS = {
    macd2_config.LONG_SYMBOL: f"레버리지({macd2_config.LONG_SYMBOL})",
    macd2_config.INVERSE_SYMBOL: f"인버스({macd2_config.INVERSE_SYMBOL})",
}

_DIRECTION_DISPLAY_LABELS = {
    "UP_RED": "레드",
    "DOWN_BLUE": "블루",
}

_EXIT_REASON_DISPLAY_LABELS = {
    macd2_config.EXIT_STOP_LOSS: "손절",
    macd2_config.EXIT_PROFIT_LOCK: "프로핏락",
    macd2_config.EXIT_OPPOSITE_SIGNAL: "반대신호",
    macd2_config.EXIT_FORCED_LIQUIDATION: "강제청산(15시)",
    macd2_config.EXIT_USER_LIQUIDATION: "수동 일괄매도",
    macd2_config.EXIT_MANUAL_LIQUIDATION: "수동 청산",
    macd2_config.EXIT_PROFIT_LOCK_MACD_CONVERGENCE: "프로핏락(MACD수렴)",
    macd2_config.EXIT_QUICK_PROFIT_TAKE_PROFIT: "퀵 익절",
    macd2_config.EXIT_TW_STOP_LOSS: "손절",
    macd2_config.EXIT_TW_TP1_PARTIAL: "1차 익절(부분)",
    macd2_config.EXIT_TW_TP2_FULL: "2차 익절(전량)",
    macd2_config.EXIT_TW_AFTER_TP1_STOP: "1차익절 후 손절",
    macd2_config.EXIT_TW_TRAILING_STOP: "트레일링 손절",
    macd2_config.EXIT_TW_AFTERNOON_TP: "오후 익절",
    macd2_config.EXIT_TW_BREAKEVEN_STOP: "본전 손절",
    macd2_config.EXIT_TW_PROFIT_LOCK_STOP: "프로핏락 손절",
    macd2_config.EXIT_EARLY_TAKE_PROFIT: "조기익절",
    macd2_config.EXIT_C1_PEAK_PROTECTION: "C1 고점보호",
    # 2026-09-21: H50/whipsaw-watch 청산을 "반대신호"와 반드시 구분한다.
    macd2_config.EXIT_H50_TREND_BREAK: "H50 휩쏘해제(추세이탈)",
    macd2_config.EXIT_H50_MAX_HOLD: "H50 보류만료",
    macd2_config.WHIPSAW_WATCH_DETERIORATION_EXIT: "휩쏘감시 악화청산",
    "RECOVERED_TO_FLAT": "청산 확인(정합화)",
    "END_OF_DATA": "데이터 종료",
}


def _symbol_display(symbol: str) -> str:
    return _SYMBOL_DISPLAY_LABELS.get(str(symbol or ""), str(symbol or "-"))


def _direction_display(direction: str) -> str:
    return _DIRECTION_DISPLAY_LABELS.get(str(direction or ""), str(direction or "-"))


def _exit_reason_display(reason: str) -> str:
    return _EXIT_REASON_DISPLAY_LABELS.get(str(reason or ""), str(reason or "-"))


def _datetime_display(raw: str) -> str:
    """날짜+시각을 한 셀에 -- YYYY-MM-DD HH:MM:SS."""
    raw = str(raw or "")
    if not raw:
        return "-"
    try:
        return datetime.fromisoformat(raw).astimezone(macd2_config.KST).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def _qty_display(raw) -> str:
    try:
        return f"{int(float(raw)):,}주"
    except (TypeError, ValueError):
        return "-"


def _price_display(raw) -> str:
    try:
        return f"{float(raw):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _money_display(raw) -> str:
    try:
        return f"{float(raw):,.0f}원"
    except (TypeError, ValueError):
        return "-"


_RECONCILE_CONTINUATION_EXIT_REASONS = {"BROKER_DIRECT", "RECOVERED_TO_FLAT", "RECOVERED_QTY_MISMATCH"}

#: 정합화 전용 행만으로 이루어진 그룹(진짜 주문 행이 하나도 합류하지 못한
#: "고아" 그룹)의 사유 표시. 체결가/손익은 reconcile 이 발견 시점 호가로 계산한
#: 추정치라 그 사실이 표에 그대로 드러나야 한다.
RECONCILE_ONLY_REASON_LABEL = "정합화(추정)"


def _is_reconcile_continuation_row(row: dict) -> bool:
    """True for a raw execution-ledger row that is NOT its own economic
    decision -- a reconcile-backfilled leg (source == RECONCILE_BACKFILL, see
    ledger.append_reconcile_backfill_buy), a BROKER_DIRECT stub confirmation
    (signal_id/exit_reason == "BROKER_DIRECT"), or a residual-cleanup sweep
    (source == RESIDUAL_CLEANUP, 2026-09-01 -- order_executor._attempt_
    residual_cleanup's own follow-up sell after an exit's reconcile left a
    tiny leftover, e.g. 1 of 809 shares). These always merge into whichever
    real order/TP-stage group they are adjacent to -- see
    _aggregate_trade_legs. The RAW ledger itself is never touched; this only
    affects how the display groups rows together."""
    if str(row.get("source") or "") in ("RECONCILE_BACKFILL", "RESIDUAL_CLEANUP"):
        return True
    if str(row.get("signal_id") or "") == "BROKER_DIRECT":
        return True
    if str(row.get("exit_reason") or "") in _RECONCILE_CONTINUATION_EXIT_REASONS:
        return True
    return False


def _economic_bucket(row: dict) -> str:
    """Grouping key for "the same real decision": every BUY leg of one entry
    shares a single bucket; a SELL leg's bucket is its OWN exit_reason, so
    TP1_PARTIAL vs a later full exit (different exit_reason values) never
    collapse into the same group even if they land inside the merge window."""
    if str(row.get("side") or "") == "BUY":
        return "BUY"
    return str(row.get("exit_reason") or "") or "SELL"


def _parse_exec_timestamp(raw) -> Optional[datetime]:
    raw = str(raw or "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=macd2_config.KST)


def _dedupe_exec_rows(exec_rows: list[dict]) -> list[dict]:
    """Drops an exec row that is a byte-for-byte re-confirmation of a fill
    already kept (same order_id/signal_id/symbol/side/qty/price/timestamp) --
    a real retry-recheck of an order in flight can log the same fill more
    than once (see worker.ORDER_FILL_RECONCILE_RETRIES). Never drops two rows
    that merely SHARE an order_id but differ in qty/price/time (a genuine
    incremental partial-fill update under the same order) -- only an exact
    duplicate. Display-only: the raw ledger itself is untouched."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in exec_rows:
        key = (
            row.get("order_id"), row.get("signal_id"), row.get("symbol"), row.get("side"),
            row.get("executed_qty"), row.get("executed_price"), row.get("timestamp"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_reconcile_restatements(exec_rows: list[dict]) -> list[dict]:
    """Drops a reconcile/BROKER_DIRECT continuation row (_is_reconcile_
    continuation_row) whose own (symbol, side, position_before, position_
    after) balance transition is IDENTICAL to a transition another row (real
    or not) already describes. Real bug fixed 2026-09-01: a real
    order_executor._record_leg row and a LATER ledger.append_reconcile_
    backfill_buy row can both get written for the SAME underlying fill (they
    carry different order_ids by construction -- the backfill's is synthetic
    from symbol/qty/avg_price/position transition, never the real KIS order
    number -- so append_execution's own order_id dedup never catches this
    pair) when reconcile notices the broker-side position increase slightly
    before the real order's own fill-confirmation polling finishes. Both rows
    then land in the same _aggregate_trade_legs group (same symbol/side,
    within gap_minutes) and get summed, silently doubling the displayed
    filled quantity (e.g. a real 1,110-share BUY showing as 2,220). A genuine
    incremental partial fill (e.g. 0->555 then 555->1110) has a DIFFERENT
    position_before/position_after pair per leg, so it is never affected by
    this filter and still sums correctly. Real (non-continuation) rows are
    always preferred and never dropped by this function; only a continuation
    row whose transition duplicates one already present is removed. Display-
    only, like _dedupe_exec_rows -- the raw ledger itself is never touched."""
    def _pos_pair(row: dict) -> Optional[tuple[int, int]]:
        pb, pa = row.get("position_before"), row.get("position_after")
        if pb in (None, "") or pa in (None, ""):
            return None
        try:
            return (int(float(pb)), int(float(pa)))
        except (TypeError, ValueError):
            return None

    covered: set[tuple] = set()
    for row in exec_rows:
        if _is_reconcile_continuation_row(row):
            continue
        pair = _pos_pair(row)
        if pair is not None:
            covered.add((str(row.get("symbol") or ""), str(row.get("side") or ""), pair))

    out: list[dict] = []
    for row in exec_rows:
        if _is_reconcile_continuation_row(row):
            pair = _pos_pair(row)
            if pair is not None:
                key = (str(row.get("symbol") or ""), str(row.get("side") or ""), pair)
                if key in covered:
                    continue  # another row (real, or an earlier continuation) already accounts for this exact transition
                covered.add(key)
        out.append(row)
    return out


def _aggregate_trade_legs(exec_rows: list[dict], *, gap_minutes: float = 2.0) -> list[dict]:
    """READ-ONLY display aggregation over the raw execution ledger -- never
    mutates/drops/merges anything in the ledger file itself (audit/recovery
    trail stays byte-for-byte intact); only groups rows for the UI summary
    table. Merges consecutive rows for the SAME symbol + SAME side into one
    group when either (a) the newer row is a reconcile/BROKER_DIRECT
    continuation row (_is_reconcile_continuation_row), which always glues
    onto whatever group it lands next to, or (b) the group's own economic
    bucket is still a placeholder (started from a continuation row with no
    real decision label yet) and this is the first real row to arrive, or
    (c) both rows share the exact same real economic bucket (_economic_
    bucket) -- AND no more than `gap_minutes` has passed since the group's
    last row. A genuinely separate decision (different exit_reason, e.g.
    TP1_PARTIAL followed by the final exit) always starts a fresh group even
    when it happens within the same `gap_minutes` window; a genuinely new,
    unrelated entry/exit almost always falls well outside the window anyway
    (flags are 3-minute-bar-confirmed at the closest).
    """
    def _sort_key(row: dict):
        ts = _parse_exec_timestamp(row.get("timestamp"))
        return ts or datetime.min.replace(tzinfo=macd2_config.KST)

    ordered = sorted(_dedupe_reconcile_restatements(_dedupe_exec_rows(exec_rows)), key=_sort_key)
    groups: list[dict] = []
    current: Optional[dict] = None
    for row in ordered:
        ts = _parse_exec_timestamp(row.get("timestamp"))
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "")
        bucket = _economic_bucket(row)
        continuation = _is_reconcile_continuation_row(row)

        same_group = False
        if current is not None and current["symbol"] == symbol and current["side"] == side:
            within_window = (
                ts is not None and current["last_ts"] is not None
                and (ts - current["last_ts"]).total_seconds() <= gap_minutes * 60
            )
            if within_window:
                if continuation or current["placeholder"]:
                    same_group = True
                else:
                    same_group = bucket == current["bucket"]

        if same_group:
            current["rows"].append(row)
            current["last_ts"] = ts or current["last_ts"]
            if not continuation:
                current["bucket"] = bucket
                current["placeholder"] = False
        else:
            if current is not None:
                groups.append(current)
            current = {
                "symbol": symbol, "side": side, "bucket": bucket,
                "placeholder": continuation, "rows": [row], "last_ts": ts,
            }
    if current is not None:
        groups.append(current)
    return groups


#: 2026-09-18 이전 행의 수수료는 옛 추정요율(0.015%) 기준이라 KIS 실측과 다르다.
#: 원장을 사후에 고쳐 쓰지 않기로 했으므로(감사 추적 보존, macd2_config.
#: REALIZED_FEE_LEDGER_CUTOFF_DATE 주석 참조) 표에서 구분만 한다.
LEGACY_FEE_MARK = "*"
LEGACY_FEE_NOTE = (
    f"{LEGACY_FEE_MARK} 표시된 수수료는 {macd2_config.REALIZED_FEE_LEDGER_CUTOFF_DATE[:4]}-"
    f"{macd2_config.REALIZED_FEE_LEDGER_CUTOFF_DATE[4:6]}-"
    f"{macd2_config.REALIZED_FEE_LEDGER_CUTOFF_DATE[6:]} 이전에 기록된 행으로, "
    "당시의 **추정 요율(0.015%)** 로 계산된 값입니다 — KIS 실제 수수료의 약 4.1배입니다. "
    "이후 기록은 KIS 실측 기준입니다. 지난 기록은 사실 보존을 위해 다시 쓰지 않습니다."
)


def _uses_legacy_fee_rate(legs: list[dict]) -> bool:
    """이 표시 그룹의 레그가 옛 추정요율 시절에 기록됐는가."""
    cutoff = str(macd2_config.REALIZED_FEE_LEDGER_CUTOFF_DATE)
    for leg in legs:
        date_key = ledger.execution_row_trading_date(leg)
        if date_key and date_key < cutoff:
            return True
    return False


def _fee_display(total_fee: float, legs: list[dict]) -> str:
    """수수료 0원과 "기록 없음"은 다르다 — KIS 도 소액 레그에 0원을 찍는다
    (2026-09-17 의 1주 6,220원 매수가 실제로 0원이었다). 어느 레그에도 fee 가
    기록되지 않은 경우(BROKER_DIRECT 스텁 등)에만 "-" 로 비운다."""
    if not any(str(leg.get("fee") or "").strip() for leg in legs):
        return "-"
    text = _money_display(total_fee)
    return f"{text}{LEGACY_FEE_MARK}" if _uses_legacy_fee_rate(legs) else text


def _trade_history_rows(exec_rows: list[dict], signal_rows: list[dict]) -> list[dict]:
    """하나의 "실제 주문/TP 단계"(같은 symbol/side/경제적 이벤트, 2분 이내
    체결)를 한 행으로 합쳐 총 체결수량/수량가중평균가/총 수수료/총 net_pnl/
    최초~최종 체결시각을 보여준다 -- 원본 execution 원장은 절대 건드리지
    않고(_aggregate_trade_legs는 읽기 전용 그룹핑), 화면 표시만 집계한다.
    TP1 partial과 최종청산처럼 실제로 서로 다른 매도 의사결정이면 언제나
    별도 그룹(별도 행)으로 남는다 (_economic_bucket 참고). signal_id로 신호
    원장과 매칭해 진입 방향(레드/블루)을 가져온다.

    2026-08-31 사용자 요청으로 RECOVERED_QTY_MISMATCH/RECONCILE_BACKFILL/
    BROKER_DIRECT 등 정합화 전용 행이 실제 주문과 2분 이내로 붙지 못해 "고아"
    그룹(group["placeholder"] == True -- 진짜 경제적 결정 행이 단 하나도
    합류하지 못한 그룹)이 되면 메인 표에서 통째로 감췄었다.

    2026-09-16 실사고로 철회한다: 그날 12:51 진입분 785주의 청산이 워커 밖에서
    나가는 바람에 SELL 레그가 정합화 backfill 3행(RECOVERED_QTY_MISMATCH x2 +
    RECOVERED_TO_FLAT)으로만 남았는데, 그 셋이 통째로 "고아" 그룹이 되어
    **실제 청산 한 건이 거래원장에서 완전히 사라졌다**(매수만 있고 매도가 없는
    표). summarize_daily_trading 은 같은 행을 거르지 않으므로 일일 통계 손익에는
    반영돼 있어서 표와 통계가 서로 어긋나기까지 했다.

    이제 감추지 않고 사유를 RECONCILE_ONLY_REASON_LABEL("정합화(추정)")로 표시
    한다 -- 체결가/손익이 reconcile 발견 시점 호가 기반 추정치라는 사실이 표에
    드러나야 하기 때문이다. 원장 자체와 손익 계산은 한 줄도 바뀌지 않는다
    (_aggregate_trade_legs 는 여전히 읽기 전용 그룹핑이다)."""
    direction_by_signal_id = {
        r.get("signal_id"): r.get("direction") for r in signal_rows if r.get("signal_id")
    }
    rows: list[dict] = []
    for group in _aggregate_trade_legs(exec_rows):
        legs = group["rows"]
        side = group["side"]
        symbol = _symbol_display(group["symbol"])

        def _leg_qty(r: dict) -> float:
            try:
                return float(r.get("executed_qty") or r.get("requested_qty") or 0)
            except (TypeError, ValueError):
                return 0.0

        def _leg_price(r: dict) -> float:
            try:
                return float(r.get("executed_price") or r.get("requested_price") or 0)
            except (TypeError, ValueError):
                return 0.0

        total_qty = sum(_leg_qty(r) for r in legs)
        weighted_price = (sum(_leg_qty(r) * _leg_price(r) for r in legs) / total_qty) if total_qty else 0.0
        total_fee = sum(float(r.get("fee") or 0.0) for r in legs)
        total_net_pnl = sum(float(r.get("net_pnl") or 0.0) for r in legs)

        first_ts = min((t for t in (_parse_exec_timestamp(r.get("timestamp")) for r in legs) if t is not None), default=None)
        last_ts = max((t for t in (_parse_exec_timestamp(r.get("timestamp")) for r in legs) if t is not None), default=None)
        if first_ts is not None and last_ts is not None and first_ts != last_ts:
            when = f"{first_ts.strftime('%Y-%m-%d %H:%M:%S')} ~ {last_ts.strftime('%H:%M:%S')}"
        else:
            when = _datetime_display(legs[0].get("timestamp"))

        if side == "BUY":
            direction = next(
                (direction_by_signal_id.get(r.get("signal_id")) for r in legs if direction_by_signal_id.get(r.get("signal_id"))),
                None,
            )
            rows.append({
                "일시": when, "종목": symbol, "매수/매도": "매수",
                "총 체결수량": _qty_display(total_qty), "체결가(수량가중평균)": _price_display(weighted_price),
                "사유": RECONCILE_ONLY_REASON_LABEL if group["placeholder"] else _direction_display(direction),
                "총 순이익": "-" if not any(r.get("net_pnl") for r in legs) else _money_display(total_net_pnl),
                "총 수수료": _fee_display(total_fee, legs),
            })
        elif side == "SELL":
            reason_label = (
                RECONCILE_ONLY_REASON_LABEL if group["placeholder"]
                else _exit_reason_display(group["bucket"])
            )
            rows.append({
                "일시": when, "종목": symbol, "매수/매도": "매도",
                "총 체결수량": _qty_display(total_qty), "체결가(수량가중평균)": _price_display(weighted_price),
                "사유": reason_label,
                "총 순이익": _money_display(total_net_pnl),
                "총 수수료": _fee_display(total_fee, legs),
            })
    return rows


try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=5000, key="macd2_refresh")
except Exception:
    pass

st.set_page_config(page_title="MACD 자동매매2", layout="wide")
st.title("MACD 자동매매2")
st.caption(
    "완전 독립 신규 모듈 · MACD v1/Enhanced와 상태·원장 미공유 · "
    "Read-only UI — command 기록과 snapshot 표시만 수행"
)

cfg = get_config()
service = get_service()
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}

col1, col2, col3 = st.columns(3)
col1.metric("Worker 상태", _worker_status(state, worker_stats))
col2.metric("모드", state.mode)
col3.metric("예산", f"{state.budget:,.0f}원")

st.subheader("계좌 / 제어")
c1, c2, c3 = st.columns([1.2, 1.2, 1])
with c1:
    mode = st.radio("계좌 모드", ["mock", "real"], index=0 if state.mode != "real" else 1, horizontal=True, key="macd2_mode")
with c2:
    budget = st.number_input(
        "투자예산 (원)", min_value=100_000, max_value=500_000_000,
        value=int(state.budget or macd2_config.DEFAULT_BUDGET), step=100_000, key="macd2_budget",
    )
with c3:
    try:
        acct = get_kis_account_config(mode)
        masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    except Exception:
        masked = None
    st.metric("계좌", masked or "(미설정)")

real_kwargs = {}
if mode == "real":
    st.error("REAL(실전) 모드 — 확인 문구 입력 후에만 시작 가능")
    expected = str(cfg.real_confirm_text() or "LIVE")
    confirm_in = st.text_input("REAL 확인 문구", type="password", key="macd2_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="macd2_real_toggle")
    real_kwargs = {
        "confirm_text": confirm_in, "runtime_real_mode": bool(real_toggle),
        "runtime_enable_real_buy": bool(real_toggle), "runtime_enable_real_sell": bool(real_toggle),
    }
else:
    st.info("MOCK 모드 (기본값) — KIS 모의투자 계좌")

b1, b2, b3, b4 = st.columns(4)
with b1:
    if st.button("자동매매 시작", type="primary", use_container_width=True):
        res = service.start(mode=mode, budget=float(budget), real_kwargs=real_kwargs if mode == "real" else None)
        if res.get("ok"):
            st.success("MACD2 자동매매 시작")
            st.rerun()
        else:
            st.error(res.get("message") or "시작 실패")
with b2:
    if st.button("자동매매 중지", use_container_width=True):
        service.stop("user_stop")
        st.warning("중지됨")
        st.rerun()
with b3:
    if st.button("자동매매 중지 및 일괄매도", use_container_width=True):
        res = service.stop_and_liquidate_all("user_stop_liquidate_all")
        sold = [r for r in res.get("results", []) if r.get("symbol")]
        if res.get("ok"):
            if sold:
                detail = ", ".join(f"{r['symbol']} {r['quantity']}주" for r in sold)
                st.warning(f"자동매매 중지 + 일괄매도 완료: {detail}")
            else:
                st.warning("자동매매 중지됨 (보유 포지션 없음)")
        else:
            failed = ", ".join(f"{r.get('symbol') or '?'}:{r.get('block_reason')}" for r in sold if not r.get("ok"))
            st.error(f"일괄매도 일부/전체 실패 — {failed or res.get('message')}")
        st.rerun()
with b4:
    if st.button("Bootstrap 재시도", use_container_width=True):
        res = service.retry_bootstrap()
        if res.get("ok"):
            st.success(res.get("message") or "Bootstrap 재시도 성공")
        else:
            st.error(res.get("message") or "Bootstrap 재시도 실패")
        st.rerun()

st.caption("수동 진입 (MACD 신호·필터 무시, 예산 내 즉시 전량매수 — 이미 보유 중이면 거부)")
m1, m2 = st.columns(2)
with m1:
    if st.button("현재시점 레버리지(레드) 전량매수", use_container_width=True):
        res = service.manual_entry("UP_RED")
        if res.get("ok"):
            st.success(f"레버리지 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"레버리지 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()
with m2:
    if st.button("현재시점 인버스(블루) 전량매수", use_container_width=True):
        res = service.manual_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success(f"인버스 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"인버스 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()

# ── 09:03 예약매수: 3-SLOT 계열에서는 노출하지 않는다 (2026-09-16 사고) ──
# H50/X2-lite/TW2 3-SLOT/TW TEG 3-SLOT 운영 중에는 이 기능을 쓰지 않는다.
# 버튼이 보이면 눌릴 수 있고, 눌리면 arm 이 생긴다 -- 그래서 렌더 자체를 막는다
# (service.arm_scheduled_entry 도 같은 조건으로 거부하므로 2중이다).
# TW/TW2/TEGv2/무필터 등 다른 전략에서는 예전과 똑같이 그대로 보인다.
_sched_supported = macd2_time_window_3slot.scheduled_entry_supported(state)
if not _sched_supported:
    st.caption(
        "09:03 예약 매수 — 현재 전략(" + str(macd2_time_window_3slot.active_3slot_mode(state) or "-")
        + ")에서는 사용하지 않습니다. 진입은 MACD 플래그 + T+3 재확인 경로로만 이뤄집니다."
    )
if _sched_supported:
    _sched_dir = getattr(state, "scheduled_entry_armed_direction", None)
    _sched_done = getattr(state, "scheduled_entry_executed_at", None)
    if _sched_done:
        _protect_note = (
            f" · 반대 플래그 보호 중(~{macd2_config.SCHEDULED_ENTRY_PROTECTION_UNTIL.strftime('%H:%M')}까지 반대신호청산 무시)"
            if getattr(state, "scheduled_entry_protected", False) else ""
        )
        st.caption(f"09:03 예약 매수 — 오늘 처리 완료: `{state.scheduled_entry_last_result or '-'}`{_protect_note}")
    else:
        _sched_label = "레버리지(레드)" if (_sched_dir and _sched_dir.value == "UP_RED") else (
            "인버스(블루)" if (_sched_dir and _sched_dir.value == "DOWN_BLUE") else "없음"
        )
        st.caption(f"09:03 예약 매수 (개장 직후 이른 플래그 대응, 하루 1회) — 현재 예약: {_sched_label}")
    sch1, sch2 = st.columns(2)
    with sch1:
        _armed_up = bool(_sched_dir and _sched_dir.value == "UP_RED")
        if st.button(
            ("[예약중] " if _armed_up else "") + "09시03분 레버리지(레드) 전량매수 예약",
            use_container_width=True, disabled=bool(_sched_done),
        ):
            res = service.arm_scheduled_entry("UP_RED")
            if res.get("ok"):
                st.success("09:03 레버리지 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
            else:
                st.error(res.get("message") or "예약 실패")
            st.rerun()
    with sch2:
        _armed_down = bool(_sched_dir and _sched_dir.value == "DOWN_BLUE")
        if st.button(
            ("[예약중] " if _armed_down else "") + "09시03분 인버스(블루) 전량매수 예약",
            use_container_width=True, disabled=bool(_sched_done),
        ):
            res = service.arm_scheduled_entry("DOWN_BLUE")
            if res.get("ok"):
                st.success("09:03 인버스 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
            else:
                st.error(res.get("message") or "예약 실패")
            st.rerun()

st.caption("수동 전량매도 (자동매매는 계속 유지, 현재 보유 포지션만 지금 즉시 매도)")
if st.button("현재 보유 포지션 수동 전량매도", use_container_width=True):
    res = service.manual_exit()
    if res.get("ok"):
        st.success(f"수동 매도 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
    else:
        st.error(f"수동 매도 실패: {res.get('message') or res.get('block_reason')}")
    st.rerun()

_qp_cols = st.columns([1.4, 1.6])
with _qp_cols[0]:
    _qp_on = st.checkbox(
        "퀵 Profit 익절",
        value=bool(getattr(state, "quick_profit_enabled", False)),
        key="macd2_quick_profit_toggle",
        help=(
            f"ON이면 보유 포지션 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절"
            "(진입 로직과 무관하게 동일 적용, 기존 손절·반대플래그청산·강제청산은 그대로 유지). 기본 OFF."
        ),
    )
with _qp_cols[1]:
    if bool(_qp_on) != bool(getattr(state, "quick_profit_enabled", False)):
        res = service.set_quick_profit_enabled(bool(_qp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"퀵 Profit 익절 → {'ON' if _qp_on else 'OFF'}")
            st.rerun()
        else:
            st.error("퀵 Profit 익절은 Profit Lock과 동시에 켤 수 없습니다.")
    else:
        st.caption(f"퀵 Profit 익절={'ON' if state.quick_profit_enabled else 'OFF'} · 문턱=+{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%")

# ── 레거시 진입전략 토글 (2026-09-07 숨김) ─────────────────────────────────
# 사용자 노출 전략을 TW2 3-SLOT / TW TEG 3-SLOT 두 개로 정리하면서 감췄다.
# 코드는 그대로 두고 렌더만 막는다 -- MACD2_SHOW_LEGACY_TW2_TOGGLES=1 이면
# 아래 블록이 예전과 똑같이 다시 렌더된다(service/worker 경로는 무관하게 상시 유지).
if macd2_config.SHOW_LEGACY_TW2_TOGGLES:
    _teg_cols = st.columns([1.4, 1.6])
    with _teg_cols[0]:
        _teg_on = st.checkbox(
            "+TEGv2",
            value=bool(getattr(state, "time_window_teg_filter_enabled", False)),
            key="macd2_time_window_teg_filter_toggle",
            disabled=not bool(getattr(state, "time_window_2_filter_enabled", False)),
            help=(
                "TW2와 완전히 동일한 T+3 재확인/품질점수/시간대/VWAP veto/최근크로스 veto 게이트+포지션관리 래더를 그대로 "
                "쓰되, 기존 TW2 하루 3회 진입한도 때문에만 거절된 후보에 한해 하루 1회 Trend Establishment Gate v2(TEGv2) 검증을 "
                "추가로 통과하면 한도를 넘어서도 진입합니다(무제한 아님, 하루 정확히 1회). TEGv2는 최근30분크로스<=1/MACD갭·"
                "EMA10-20스프레드 signed 순증가/가격-EMA스택 정렬/세션VWAP 유리한 쪽/직전반대플래그 9분 이상 경과를 모두 "
                "요구합니다. 2026-06-01~08-26 60거래일 TRAIN(40일, 임계값 보정용)/OOS(20일, 미사용) 분할검증에서 OOS 4개 "
                "지표(거래수/총수익/복리/MDD) 전부 개선 확인. TW2의 선택형 보조필터이며 +1 DOWN_BLUE와 독립적으로 켤 수 있습니다. 기본 OFF."
            ),
        )
    with _teg_cols[1]:
        if bool(_teg_on) != bool(getattr(state, "time_window_teg_filter_enabled", False)):
            res = service.set_time_window_teg_filter_enabled(bool(_teg_on), changed_by="ui")
            if res.get("ok"):
                _sync_tier_toggle_widgets(res, skip="macd2_time_window_teg_filter_toggle")
                st.caption(f"+TEGv2 → {'ON' if _teg_on else 'OFF'}")
                st.rerun()
        else:
            st.caption(
                f"+TEGv2={'ON' if state.time_window_teg_filter_enabled else 'OFF'} · "
                f"오전 진입 {int(getattr(state, 'time_window_morning_entry_count', 0) or 0)}/{macd2_config.MAX_MORNING_ENTRIES} · "
                f"오후 진입 {int(getattr(state, 'time_window_afternoon_entry_count', 0) or 0)}/{macd2_config.MAX_AFTERNOON_ENTRIES} · "
                f"오늘 추가진입 사용={'Y' if getattr(state, 'time_window_teg_count_cap_bypass_used', False) else '-'} · "
                f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
                + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
            )
            if getattr(state, "last_time_window_teg_candidate_at", None):
                st.caption(
                    "최근 TEGv2 후보: "
                    f"{'승인' if getattr(state, 'last_time_window_teg_approved', False) else '거절'} · "
                    f"사유={', '.join(getattr(state, 'last_time_window_teg_reject_reasons', []) or ['-'])}"
                )

# ── 상호배타 tier 토글 위젯 동기화 (2026-09-07) ─────────────────────────────
# TW2 / +TEGv2 / TW2 3-SLOT / TW TEG 3-SLOT 은 서로를 끈다. 하나를 켜면 서버쪽
# 상태에서 나머지가 꺼지는데, 체크박스 **위젯** 상태는 그대로 True 로 남는다.
# 그러면 st.rerun() 직후 그 토글이 "위젯 True vs 상태 False" 를 자기 변경으로
# 착각해 스스로를 다시 켜고, 방금 켠 토글을 도로 끈다(ping-pong). 그래서 setter
# 가 성공하면 **방금 만진 것 말고 나머지** 위젯 상태를 서버가 돌려준 실제 값으로
# 맞춘 뒤 rerun 한다. 판단 로직이 아니라 위젯 상태 정리다.
_TIER_TOGGLE_WIDGETS = {
    "macd2_time_window_2_filter_toggle": "time_window_2_filter_enabled",
    "macd2_time_window_teg_filter_toggle": "time_window_teg_filter_enabled",
    "macd2_time_window_3slot_filter_toggle": "time_window_3slot_filter_enabled",
    "macd2_time_window_twf_filter_toggle": "time_window_twf_filter_enabled",
    "macd2_time_window_x2lite_filter_toggle": "time_window_x2lite_filter_enabled",
    "macd2_time_window_h50_filter_toggle": "time_window_h50_filter_enabled",
    "macd2_time_window_n1_filter_toggle": "time_window_n1_filter_enabled",
}


def _sync_tier_toggle_widgets(res: dict, *, skip: str) -> None:
    # 이미 렌더된 위젯의 session_state 에 **대입**하면 Streamlit 이
    # "cannot be modified after the widget is instantiated" 로 막는다.
    # 대신 키를 지운다 -- 다음 rerun 에서 체크박스가 value=(실제 상태) 로
    # 다시 초기화되므로 stale 위젯이 스스로를 되켜는 일이 없어진다.
    for _wkey, _field in _TIER_TOGGLE_WIDGETS.items():
        if _wkey == skip or _field not in res:
            continue
        if bool(st.session_state.get(_wkey, False)) != bool(res[_field]):
            st.session_state.pop(_wkey, None)


# ── 레거시 진입전략 토글 (2026-09-07 숨김) ─────────────────────────────────
# 사용자 노출 전략을 TW2 3-SLOT / TW TEG 3-SLOT 두 개로 정리하면서 감췄다.
# 코드는 그대로 두고 렌더만 막는다 -- MACD2_SHOW_LEGACY_TW2_TOGGLES=1 이면
# 아래 블록이 예전과 똑같이 다시 렌더된다(service/worker 경로는 무관하게 상시 유지).
if macd2_config.SHOW_LEGACY_TW2_TOGGLES:
    _tw2_cols = st.columns([1.4, 1.6])
    with _tw2_cols[0]:
        _tw2_on = st.checkbox(
            "시간대별 최적거래 필터 (TW2)",
            value=bool(getattr(state, "time_window_2_filter_enabled", False)),
            key="macd2_time_window_2_filter_toggle",
            help=(
                "TW1과 완전히 동일한 T+3 재확인/품질점수/시간대/최대진입횟수 게이트+포지션관리 래더를 그대로 쓰되, "
                "진입 시점에 두 가지 veto를 추가로 검사합니다: ① 확정봉 종가가 진입방향 기준 세션 VWAP보다 "
                f"{abs(macd2_config.TW2_VWAP_VETO_THRESHOLD_PCT):.1f}% 이상 불리한 쪽이면 스킵, ② 최근 "
                f"{macd2_config.TW2_RECENT_CROSS_LOOKBACK_MINUTES}분 안에 확정 크로스오버가 "
                f"{macd2_config.TW2_RECENT_CROSS_VETO_COUNT}회 이상이면(휩쏘 구간) 스킵. 통과한 진입은 TP2만 "
                f"+{macd2_config.MORNING_TP2*100:.1f}%→+{macd2_config.TW2_MORNING_TP2*100:.1f}%로 올립니다. "
                "2026-07-10~08-21 연속 29거래일, 앞 19일에서 찾은 임계값을 뒤 10일 OOS에서 재튜닝 없이 재검증 "
                "(복리 +24.5%→+35.7%, MDD 6.8%→5.2%). 기본 ON. +TEGv2와 +1 DOWN_BLUE는 독립 선택형 보조필터입니다."
            ),
        )
    with _tw2_cols[1]:
        if bool(_tw2_on) != bool(getattr(state, "time_window_2_filter_enabled", False)):
            res = service.set_time_window_2_filter_enabled(bool(_tw2_on), changed_by="ui")
            if res.get("ok"):
                _sync_tier_toggle_widgets(res, skip="macd2_time_window_2_filter_toggle")
                st.caption(f"시간대별 최적거래 필터 (TW2) → {'ON' if _tw2_on else 'OFF'}")
                st.rerun()
        else:
            st.caption(
                f"TW2={'ON' if state.time_window_2_filter_enabled else 'OFF'} · "
                f"오전 진입 {int(getattr(state, 'time_window_morning_entry_count', 0) or 0)}/{macd2_config.MAX_MORNING_ENTRIES} · "
                f"오후 진입 {int(getattr(state, 'time_window_afternoon_entry_count', 0) or 0)}/{macd2_config.MAX_AFTERNOON_ENTRIES} · "
                f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
                + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
            )

# ── TW2 3-SLOT / TW TEG 3-SLOT 토글 숨김 (2026-09-15 사용자 요청) ─────────
# 노출 전략을 'X2-lite + W1a' / 'X2-lite + W1a + H50' 로 정리하면서 감췄다.
# 코드는 그대로 두고 렌더만 막는다 -- MACD2_SHOW_LEGACY_3SLOT_TOGGLES=1 이면
# 아래 두 블록이 예전과 똑같이 다시 렌더된다(service/worker 경로는 상시 유지).
# 숨긴 상태에서 켜져 있으면 아래 경고가 뜬다(활성 전략이 안 보이는 일 방지).
if not macd2_config.SHOW_LEGACY_3SLOT_TOGGLES:
    _hidden_on = [
        _n for _n, _f in (("TW2 3-SLOT", "time_window_3slot_filter_enabled"),
                          (macd2_config.TW_TEG_3SLOT_STRATEGY_NAME, "time_window_twf_filter_enabled"))
        if bool(getattr(state, _f, False))
    ]
    if _hidden_on:
        st.warning(
            "⚠ 숨긴 전략이 켜져 있습니다: " + " · ".join(_hidden_on)
            + " — 끄려면 MACD2_SHOW_LEGACY_3SLOT_TOGGLES=1 로 토글을 다시 노출하세요."
        )
if macd2_config.SHOW_LEGACY_3SLOT_TOGGLES:
    _3slot_cols = st.columns([1.4, 1.6])
    with _3slot_cols[0]:
        _3slot_on = st.checkbox(
            "TW2 3-SLOT",
            value=bool(getattr(state, "time_window_3slot_filter_enabled", False)),
            key="macd2_time_window_3slot_filter_toggle",
            help=(
                "TW2/TEGv2와 완전히 동일한 T+3 재확인/VWAP·최근크로스 veto/TP1·TP2·trailing·손절/휩쏘-내성 반대신호청산을 "
                "그대로 쓰되, 하루 신규진입을 정확히 3회로 제한하고 슬롯 배분만 새로 짭니다: 09:00-11:00 1·2번째는 TW2 승인만, "
                "3번째는 Trend Quality 5개 조건(가격/EMA10 방향·EMA10-20 signed 스프레드 확대·MACD갭 확대·EMA20 기울기·VWAP 방향) "
                f"중 {macd2_config.TW2_3SLOT_MORNING_3RD_QUALITY_MIN}개 이상 통과해야 사용, 실패하면 그 슬롯은 오후로 이월됩니다. "
                "11:00-14:50은 남은 슬롯이 있을 때만 TW2 승인 AND TEGv2 승인을 모두 요구하고, 2번째 오후 진입은 직전 오후 포지션이 "
                "종료된 뒤 반대 방향일 때만 허용합니다. 60거래일 TRAIN(40)/OOS(20) 백테스트+2차 독립 시뮬레이션 교차검증에서 현행 "
                "TW2 대비 OOS 복리·PF·MDD·Top10제외수익 전부 개선 확인(data/validation/tw2_3slot_flex/). TW2/+TEGv2와 동시에 켤 수 "
                "없습니다(셋 중 하나만). 기본 OFF — 실거래 검증 후 기본값 변경 여부를 결정합니다."
            ),
        )
    with _3slot_cols[1]:
        if bool(_3slot_on) != bool(getattr(state, "time_window_3slot_filter_enabled", False)):
            res = service.set_time_window_3slot_filter_enabled(bool(_3slot_on), changed_by="ui")
            if res.get("ok"):
                _sync_tier_toggle_widgets(res, skip="macd2_time_window_3slot_filter_toggle")
                st.caption(f"TW2 3-SLOT → {'ON' if _3slot_on else 'OFF'}")
                st.rerun()
        else:
            st.caption(
                f"TW2 3-SLOT={'ON' if state.time_window_3slot_filter_enabled else 'OFF'} · "
                f"오늘 슬롯 {int(getattr(state, 'tw2_3slot_slots_used_today', 0) or 0)}/{macd2_config.TW2_3SLOT_DAILY_CAP} "
                f"(오전 {int(getattr(state, 'tw2_3slot_morning_count', 0) or 0)} · 오후 {int(getattr(state, 'tw2_3slot_afternoon_count', 0) or 0)}) · "
                f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
                + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
            )
            if getattr(state, "last_tw2_3slot_signal_id", None):
                st.caption(
                    "최근 TW2 3-SLOT 후보: "
                    f"{'승인' if getattr(state, 'last_tw2_3slot_approved', False) else '거절'} · "
                    f"슬롯={getattr(state, 'last_tw2_3slot_slot_number', '-') or '-'} · "
                    f"사유={getattr(state, 'last_tw2_3slot_block_reason', None) or '-'}"
                )

# ── TW TEG 3-SLOT (2026-09-08, 구 "TWF 3-SLOT") ────────────────────────────
# TW2 3-SLOT 의 자매 전략. 진입 판정 경로/슬롯 카운터를 그대로 공유하고,
# 다른 것은 (1) 청산 임계값 3개 (2) CHOP 후보에 TEGv2 추가 요구, 둘뿐이다.
# 내부 식별자(state.time_window_twf_filter_enabled / MODE_TWF_3SLOT)는 저장된
# 상태·원장 호환 때문에 그대로 두고 표시 이름만 바꿨다.
if macd2_config.SHOW_LEGACY_3SLOT_TOGGLES:
    _twf_cols = st.columns([1.4, 1.6])
    with _twf_cols[0]:
        _twf_on = st.checkbox(
            macd2_config.TW_TEG_3SLOT_STRATEGY_NAME,
            value=bool(getattr(state, "time_window_twf_filter_enabled", False)),
            key="macd2_time_window_twf_filter_toggle",
            help=(
                "TW2 3-SLOT 과 MACD zero-cross/T+3 재확인/TW2 veto/슬롯 배분/Trend Quality/TEGv2/"
                "휩쏘-내성 반대신호청산이 전부 동일하고, 하루 신규진입 3회 상한과 '오전에 남은 슬롯만 "
                "13:00~14:50 에 사용' 규칙도 그대로입니다. "
                "**진입에서 다른 것은 하나뿐입니다**: 진입 확정봉이 CHOP(횡보/휩쏘)으로 분류된 후보만 "
                "TEGv2 를 추가로 통과해야 진입합니다. 통과하면 대기 없이 즉시 진입하고, 실패하면 그 후보만 "
                "취소되며 **슬롯은 소비되지 않습니다**(다음 플래그가 같은 슬롯으로 다시 평가됩니다). "
                "CHOP 이 아닌 후보는 기존과 100% 동일하게 즉시 진입합니다. 오후 슬롯은 원래 TEGv2 를 "
                "요구하므로 실효는 '오전 CHOP 후보에도 TEGv2 요구'입니다. "
                "⚠ 아래 수치는 **잠정(provisional)** 입니다 — 사후조회 프리마켓 봉 기반이고 그 입력의 "
                "production 신호 재현율이 42.9% 로 측정됐습니다. 프리마켓 재현성 확보 후 재검증 전까지 "
                "확정 성과로 보지 마세요. "
                "잠정검증(faithful-fill, 68영업일 20260528~20260907): 복리 +101.32%→+132.00%, PF 1.566→1.834, "
                "MDD -10.24%→-8.50%, 하루 3회 cap 위반 0일. 차단 21건 전부 오전 CHOP 후보이고 전부 TEGv2 "
                "탈락(막은 손실 -31.03% / 놓친 수익 +16.44%), 공통 147거래 손익 변동 0건. "
                "단 +3% 러너 2건을 놓쳤고(러너보존 93.3%) TEGv2 임계값은 원래 오후용이라 오전 적용은 "
                "이번이 첫 데이터입니다 — 그 리스크를 알고 켜세요. "
                "**청산 임계값 3개**도 TW2 3-SLOT 과 다릅니다: 오전 손절 "
                f"-1.7% → -{abs(macd2_config.TWF_MORNING_STOP_LOSS) * 100:.1f}%, TP1 이후 잔량 스탑 +0.3% → "
                f"+{macd2_config.TWF_MORNING_AFTER_TP1_STOP * 100:.1f}%, 오후 TP +2.5% → "
                f"+{macd2_config.TWF_AFTERNOON_TP * 100:.1f}%. TP1/분할비율/TP2/trailing/조기익절은 TW2 3-SLOT 과 동일합니다. "
                "TW2 3-SLOT 과 동시에 켤 수 없습니다(둘 중 하나만). 기본 OFF."
            ),
        )
    with _twf_cols[1]:
        if bool(_twf_on) != bool(getattr(state, "time_window_twf_filter_enabled", False)):
            res = service.set_time_window_twf_filter_enabled(bool(_twf_on), changed_by="ui")
            if res.get("ok"):
                _sync_tier_toggle_widgets(res, skip="macd2_time_window_twf_filter_toggle")
                st.caption(f"{macd2_config.TW_TEG_3SLOT_STRATEGY_NAME} → {'ON' if _twf_on else 'OFF'}")
                st.rerun()
        else:
            st.caption(
                f"{macd2_config.TW_TEG_3SLOT_STRATEGY_NAME}={'ON' if state.time_window_twf_filter_enabled else 'OFF'} · "
                f"오늘 슬롯 {int(getattr(state, 'tw2_3slot_slots_used_today', 0) or 0)}/{macd2_config.TW2_3SLOT_DAILY_CAP} "
                f"(오전 {int(getattr(state, 'tw2_3slot_morning_count', 0) or 0)} · 오후 {int(getattr(state, 'tw2_3slot_afternoon_count', 0) or 0)}) · "
                f"청산 SL -{abs(macd2_config.TWF_MORNING_STOP_LOSS) * 100:.1f}% / TP1후 +{macd2_config.TWF_MORNING_AFTER_TP1_STOP * 100:.1f}% / 오후TP +{macd2_config.TWF_AFTERNOON_TP * 100:.1f}% · "
                f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
                + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
            )

# ── X2-lite (2026-09-12) ───────────────────────────────────────────────────
# TW TEG 3-SLOT 과 **진입이 100% 동일한** 자매 전략. 다른 것은 청산 파라미터와
# 내장 조기익절뿐이다(config.py X2LITE_* 블록 / time_window_3slot.exit_overrides).
_x2lite_cols = st.columns([1.4, 1.6])
with _x2lite_cols[0]:
    _x2lite_on = st.checkbox(
        f"{macd2_config.X2LITE_3SLOT_STRATEGY_NAME} + {macd2_config.X2LITE_SIZING_NAME} sizing",
        value=bool(getattr(state, "time_window_x2lite_filter_enabled", False)),
        key="macd2_time_window_x2lite_filter_toggle",
        help=(
            f"진입은 {macd2_config.TW_TEG_3SLOT_STRATEGY_NAME} 과 **100% 동일**합니다 — MACD 플래그 탐지/"
            "T+3 재확인/TEGv2/Trend Quality/슬롯 배분/하루 3회 cap/신규진입 cutoff/"
            "same-direction·opposite-direction 규칙/신호원장 propagation 이 한 줄도 다르지 않고, "
            "CHOP 후보에 TEGv2 를 추가로 요구하는 규칙까지 그대로입니다. "
            "**다른 것은 청산 파라미터뿐입니다**: TP1 분할 50%→20%, trailing 2.0%→2.8%, "
            f"TP2 6.0%→{macd2_config.X2LITE_MORNING_TP2 * 100:.1f}%, 오전 손절 -1.4%→"
            f"-{abs(macd2_config.X2LITE_MORNING_STOP_LOSS) * 100:.1f}%. "
            f"TP1 트리거 {macd2_config.MORNING_TP1 * 100:.1f}% / TP1이후 잔량 stop "
            f"+{macd2_config.X2LITE_MORNING_AFTER_TP1_STOP * 100:.1f}% / 오후 TP "
            f"+{macd2_config.X2LITE_AFTERNOON_TP * 100:.1f}% 는 동일합니다. "
            f"**조기익절(ETP)이 내장돼 자동 ON** 입니다 (trigger +{macd2_config.X2LITE_EARLY_TP_TRIGGER_PCT:.1f}% / "
            f"floor +{macd2_config.X2LITE_EARLY_TP_FLOOR_PCT:.1f}%) — 아래 '조기익절 필터' 토글은 이 모드에서 "
            "참조되지 않으므로 중복 적용되지 않습니다. 휩쏘/반대신호/강제청산/profit-lock 등 나머지 청산 "
            "로직은 전부 기존 그대로입니다. "
            f"**주문수량에 {macd2_config.X2LITE_SIZING_NAME} sizing 이 자동 적용**됩니다 — 진입/청산 판정은 "
            "한 줄도 바뀌지 않고 수량만 조절합니다: 진입 확정봉이 CHOP 이면 80%, 그날 첫 거래가 "
            "STOP_LOSS 로 끝난 뒤의 진입은 120%, 둘 다면 96%. 단일 거래 25~150%, 하루 누적 "
            "exposure 300% 상한(초과분은 마지막 슬롯 수량이 줄어듭니다). 별도 sizing 토글은 없으므로 "
            "중복 적용되지 않습니다. "
            "⚠ 잠정(provisional) — 사후조회 프리마켓 봉 기반이고 production 신호 재현율이 42.9% 로 "
            "측정됐습니다. 확정 성과로 보지 마세요. "
            "검증(faithful-fill, 70영업일 20260527~20260908, 진입 172건 전부 F 와 동일, "
            "data/validation/exit_uplift_20260911/x2lite_final/): "
            "70일 복리 +116.08%→+146.30%, PF 1.614→1.729, MDD -10.24%→-9.85%. "
            "최근30일 +43.71%→+59.05%, PF 1.663→1.865, MDD -8.02%. "
            "10개 창 전부 X1 초과, walk-forward 6fold 중 5fold X1 초과, "
            "paired bootstrap 10,000회 X1 대비 우위확률 99.52~99.90%. "
            "다른 전략과 동시에 켤 수 없습니다. 기본 OFF."
        ),
    )
with _x2lite_cols[1]:
    if bool(_x2lite_on) != bool(getattr(state, "time_window_x2lite_filter_enabled", False)):
        res = service.set_time_window_x2lite_filter_enabled(bool(_x2lite_on), changed_by="ui")
        if res.get("ok"):
            _sync_tier_toggle_widgets(res, skip="macd2_time_window_x2lite_filter_toggle")
            st.caption(f"{macd2_config.X2LITE_3SLOT_STRATEGY_NAME} → {'ON' if _x2lite_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"{macd2_config.X2LITE_3SLOT_STRATEGY_NAME}={'ON' if state.time_window_x2lite_filter_enabled else 'OFF'} · "
            f"오늘 슬롯 {int(getattr(state, 'tw2_3slot_slots_used_today', 0) or 0)}/{macd2_config.TW2_3SLOT_DAILY_CAP} "
            f"(오전 {int(getattr(state, 'tw2_3slot_morning_count', 0) or 0)} · 오후 {int(getattr(state, 'tw2_3slot_afternoon_count', 0) or 0)}) · "
            f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
            + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
        )
if bool(getattr(state, "time_window_x2lite_filter_enabled", False)):
    st.caption(
        f"└ 진입: {macd2_config.TW_TEG_3SLOT_STRATEGY_NAME} 동일 · "
        f"TP1 {macd2_config.MORNING_TP1 * 100:.1f}% / {macd2_config.X2LITE_MORNING_TP1_SELL_RATIO * 100:.0f}% · "
        f"Trail {macd2_config.X2LITE_MORNING_TRAILING_STOP * 100:.1f}% · "
        f"TP2 {macd2_config.X2LITE_MORNING_TP2 * 100:.1f}% · "
        f"오전 SL -{abs(macd2_config.X2LITE_MORNING_STOP_LOSS) * 100:.1f}% · "
        f"ETP trigger {macd2_config.X2LITE_EARLY_TP_TRIGGER_PCT:.1f}% / floor {macd2_config.X2LITE_EARLY_TP_FLOOR_PCT:.1f}% "
        "(**ETP 포함** — 자동 ON)"
    )
    st.caption(
        f"└ 수량: **{macd2_config.X2LITE_SIZING_NAME} sizing 자동 적용** — "
        f"CHOP {macd2_config.X2LITE_SIZING_CHOP_MULT * 100:.0f}% / "
        f"그날 첫 거래 손절 이후 {macd2_config.X2LITE_SIZING_POST_STOP_MULT * 100:.0f}% "
        f"(둘 다 {macd2_config.X2LITE_SIZING_CHOP_MULT * macd2_config.X2LITE_SIZING_POST_STOP_MULT * 100:.0f}%) · "
        f"단일 {macd2_config.X2LITE_SIZING_MIN_MULT * 100:.0f}~{macd2_config.X2LITE_SIZING_MAX_MULT * 100:.0f}% · "
        f"일일 exposure 상한 {macd2_config.X2LITE_SIZING_DAILY_EXPOSURE_CAP * 100:.0f}% · "
        f"오늘 사용 {float(getattr(state, 'x2lite_exposure_used_today', 0.0) or 0.0) * 100:.0f}%"
        + (" · 첫 거래 손절 확정" if getattr(state, 'x2lite_first_trade_stop_loss', False) else "")
    )

# ── H50 : X2-lite + W1a + 작은휩쏘 HOLD (2026-09-15) ────────────────────────
# X2-lite 와 **진입·청산 파라미터·사이징이 100% 동일**하고, 반대신호 청산을
# 조건부로 보류하는 것 하나만 다르다(app/trading/macd2/small_whipsaw_hold.py).
_h50_cols = st.columns([1.4, 1.6])
with _h50_cols[0]:
    _h50_on = st.checkbox(
        macd2_config.H50_3SLOT_STRATEGY_NAME,
        value=bool(getattr(state, "time_window_h50_filter_enabled", False)),
        key="macd2_time_window_h50_filter_toggle",
        help=(
            f"진입은 {macd2_config.X2LITE_3SLOT_STRATEGY_NAME} 과 **100% 동일**합니다 — MACD 플래그 탐지/"
            "T+3 재확인/TEGv2/Trend Quality/슬롯 배분/하루 3회 cap/신규진입 cutoff/CHOP TEGv2 재게이트/"
            f"{macd2_config.X2LITE_SIZING_NAME} sizing 이 한 줄도 다르지 않습니다. "
            "청산 파라미터(TP1 20% / trailing 2.8% / TP2 5.0% / 오전손절 -1.3% / ETP 내장)도 동일합니다. "
            "**H50 때문에 신규 진입이 추가되거나 삭제되지 않습니다.** "
            "\n\n**다른 것은 반대신호 청산 하나뿐입니다**: 보유 중 반대 플래그가 정상 확정됐을 때 "
            f"(a) 보유방향이 구조적 상위추세와 같고 (LONG: EMA{macd2_config.H50_TREND_EMA_FAST}>"
            f"EMA{macd2_config.H50_TREND_EMA_SLOW} / SHORT: 반대) "
            f"(b) 최근 {macd2_config.H50_RANGE_BARS * 3}분 high-low range ≤ {macd2_config.H50_RANGE_MAX_PCT:.2f}% "
            "이면 그 청산을 **보류(HOLD)** 합니다. "
            f"\n\nHOLD 중에도 하드스톱 -{abs(macd2_config.X2LITE_MORNING_STOP_LOSS) * 100:.1f}% / TP1 / TP2 / "
            "트레일링 / ETP / 강제청산은 **전부 그대로** 작동하고, 반대방향 신규진입은 하지 않습니다. "
            f"해제는 추세가 반대로 {macd2_config.H50_TREND_BREAK_BARS}개 완성 3분봉 연속 전환되거나 "
            f"HOLD 시작 후 {macd2_config.H50_MAX_HOLD_MIN}분이 지나면 일어나고, 그 뒤엔 기존 "
            f"{macd2_config.X2LITE_3SLOT_STRATEGY_NAME} 로직으로 그대로 복귀합니다. "
            "\n\n⚠ 잠정(provisional) — 사후조회 프리마켓 봉 기반이고 production 신호 재현율이 42.9% 로 "
            "측정됐습니다. 확정 성과로 보지 마세요. "
            "검증(faithful-fill, data/validation/macd2/small_whipsaw_hold_20260915 · h50_stress_20260915): "
            "최근30일 +58.66%→+66.53%, PF 2.012→2.090, MDD -6.40%→-6.27%. "
            "최근70일 +151.22%→+184.76%, PF 1.782→1.854, MDD -9.87% 동일. "
            "2D 파라미터 grid 35칸 전부 기준 초과(plateau), walk-forward 5/6, "
            "슬리피지 +0.30%p 에서도 우위 유지. "
            "**다만 bootstrap 94.9% 로 95% 기준에 미달해 등급은 ADOPT 가 아니라 BORDERLINE 입니다.** "
            "소액/섀도우로 먼저 검증하시길 권합니다. "
            "다른 전략과 동시에 켤 수 없습니다. 기본 OFF."
        ),
    )
with _h50_cols[1]:
    if bool(_h50_on) != bool(getattr(state, "time_window_h50_filter_enabled", False)):
        res = service.set_time_window_h50_filter_enabled(bool(_h50_on), changed_by="ui")
        if res.get("ok"):
            _sync_tier_toggle_widgets(res, skip="macd2_time_window_h50_filter_toggle")
            st.caption(f"{macd2_config.H50_3SLOT_STRATEGY_NAME} → {'ON' if _h50_on else 'OFF'}")
            st.rerun()
    else:
        _holding = bool(getattr(state, "h50_hold_active", False))
        st.caption(
            f"H50={'ON' if state.time_window_h50_filter_enabled else 'OFF'} · "
            f"HOLD={'진행중' if _holding else '-'}"
            + (f" ({getattr(state, 'h50_original_direction', '') or ''})" if _holding else "")
        )
if bool(getattr(state, "time_window_h50_filter_enabled", False)):
    st.caption(
        f"└ 진입/청산/사이징: {macd2_config.X2LITE_3SLOT_STRATEGY_NAME} + "
        f"{macd2_config.X2LITE_SIZING_NAME} 과 **완전 동일** · "
        f"HOLD 조건: EMA{macd2_config.H50_TREND_EMA_FAST}/EMA{macd2_config.H50_TREND_EMA_SLOW} 순방향 "
        f"AND 최근 {macd2_config.H50_RANGE_BARS * 3}분 range ≤ {macd2_config.H50_RANGE_MAX_PCT:.2f}% · "
        f"해제: 추세 {macd2_config.H50_TREND_BREAK_BARS}봉 연속 반전 또는 {macd2_config.H50_MAX_HOLD_MIN}분 경과"
    )
    if bool(getattr(state, "h50_hold_active", False)):
        _rng = getattr(state, "h50_last_hold_range_pct", None)
        st.warning(
            f"🔒 H50 HOLD 진행중 — 보유 {getattr(state, 'h50_original_direction', '') or '?'} · "
            f"시작 {(getattr(state, 'h50_hold_started_at', '') or '')[11:19]} · "
            f"추세반전 {int(getattr(state, 'h50_trend_break_count', 0) or 0)}/"
            f"{macd2_config.H50_TREND_BREAK_BARS}봉"
            + (f" · 진입시 range {float(_rng):.2f}%" if _rng is not None else "")
            + f" · 하드스톱/TP/트레일링/ETP/강제청산은 정상 작동"
        )

# ── N1 (2026-09-20) ────────────────────────────────────────────────────────
# X2-lite / H50 과 같은 tier 의 상호배타 전략이다. 진입은 H50 과 동일하고
# quality 임계값만 3(H50 은 4), 청산은 상위추세 여부로 TP1/TP1비중/TP2 가
# 완성봉마다 8%↔4% 로 전환된다(app/trading/macd2/n1_adaptive.py).
_n1_cols = st.columns([1.4, 1.6])
with _n1_cols[0]:
    _n1_on = st.checkbox(
        macd2_config.N1_3SLOT_STRATEGY_NAME,
        value=bool(getattr(state, "time_window_n1_filter_enabled", False)),
        key="macd2_time_window_n1_filter_toggle",
        help=(
            "**N1 adaptive TP2 8%↔4%** — X2-lite/H50 과 같은 tier 의 상호배타 전략입니다. "
            "진입은 H50 과 동일한 코드(플래그/T+3/TW2 veto/슬롯/TEGv2/CHOP 게이트/W1a 사이징)를 "
            f"그대로 쓰고 **창별 quality 기준점수만 {macd2_config.QUALITY_SCORE_THRESHOLD} → "
            f"{macd2_config.N1_QUALITY_SCORE_THRESHOLD}** 로 완화합니다 — 그래서 진입집합이 H50 과 "
            "다릅니다(78영업일 H50 157거래 vs N1 158거래). "
            "청산은 보유방향 기준 상위추세(close/EMA"
            f"{macd2_config.H50_TREND_EMA_FAST}/EMA{macd2_config.H50_TREND_EMA_SLOW}/기울기, "
            "H50 과 **같은 EMA 상수**) 여부로 **완성 3분봉마다** 전환됩니다. "
            f"추세: TP1 {macd2_config.N1_TREND_TP1*100:.1f}% / TP1매도비중 "
            f"{macd2_config.N1_TREND_TP1_SELL_RATIO:.0%} / **TP2 {macd2_config.N1_TREND_TP2*100:.0f}%**. "
            f"비추세: TP1 {macd2_config.MORNING_TP1*100:.1f}% / TP1매도비중 "
            f"{macd2_config.X2LITE_MORNING_TP1_SELL_RATIO:.0%} / **TP2 "
            f"{macd2_config.N1_OFF_TREND_TP2*100:.0f}%**. "
            f"고정값: 손절 {macd2_config.X2LITE_MORNING_STOP_LOSS*100:.2f}% / after-TP1 "
            f"{macd2_config.X2LITE_MORNING_AFTER_TP1_STOP*100:.2f}% / 트레일링 트리거 "
            f"{macd2_config.MORNING_TRAILING_TRIGGER*100:.1f}% → 스탑 "
            f"**{macd2_config.N1_MORNING_TRAILING_STOP*100:.2f}%**(X2-lite 2.80%) / 오후TP "
            f"**{macd2_config.N1_AFTERNOON_TP*100:.2f}%**(X2-lite 3.00%) / 조기익절 "
            f"{macd2_config.N1_EARLY_TP_TRIGGER_PCT:.1f}% → **{macd2_config.N1_EARLY_TP_FLOOR_PCT:.1f}%**"
            "(X2-lite 1.0%) / 강제청산 15:00 불변. "
            "small whipsaw HOLD 는 H50 로직·상수를 그대로 씁니다. "
            "검증(data/validation/macd2/n1_production_20260920/): 78영업일 0527~0918 "
            "**158거래 / 복리 401.0853%**, PF 2.587, MDD −8.91% — 연구엔진이 이 코드의 "
            "production 정의를 읽어 진입·청산 parity diff 0 으로 재현했습니다. "
            "다른 전략과 동시에 켤 수 없습니다. 기본 OFF."
        ),
    )
with _n1_cols[1]:
    if bool(_n1_on) != bool(getattr(state, "time_window_n1_filter_enabled", False)):
        res = service.set_time_window_n1_filter_enabled(bool(_n1_on), changed_by="ui")
        if res.get("ok"):
            _sync_tier_toggle_widgets(res, skip="macd2_time_window_n1_filter_toggle")
            st.caption(f"{macd2_config.N1_3SLOT_STRATEGY_NAME} → {'ON' if _n1_on else 'OFF'}")
            st.rerun()
    else:
        _rg = str(getattr(state, "n1_regime_state", "") or "")
        _etp2 = getattr(state, "n1_effective_tp2", None)
        st.caption(
            f"N1={'ON' if getattr(state, 'time_window_n1_filter_enabled', False) else 'OFF'} · "
            f"adaptive TP2 {macd2_config.N1_TREND_TP2*100:.0f}%↔{macd2_config.N1_OFF_TREND_TP2*100:.0f}%"
            + (f" · 현재 {_etp2:.1f}% ({_rg})" if _etp2 is not None and _rg else "")
        )
if bool(getattr(state, "time_window_n1_filter_enabled", False)):
    st.caption(
        f"└ 진입: {macd2_config.H50_3SLOT_STRATEGY_NAME} 과 동일 + quality 기준 "
        f"{macd2_config.N1_QUALITY_SCORE_THRESHOLD}(H50 {macd2_config.QUALITY_SCORE_THRESHOLD}) · "
        f"청산: 추세 TP1 {macd2_config.N1_TREND_TP1*100:.1f}%/비중 "
        f"{macd2_config.N1_TREND_TP1_SELL_RATIO:.0%}/TP2 {macd2_config.N1_TREND_TP2*100:.0f}% ↔ "
        f"비추세 TP1 {macd2_config.MORNING_TP1*100:.1f}%/비중 "
        f"{macd2_config.X2LITE_MORNING_TP1_SELL_RATIO:.0%}/TP2 "
        f"{macd2_config.N1_OFF_TREND_TP2*100:.0f}% · 트레일링 스탑 "
        f"{macd2_config.N1_MORNING_TRAILING_STOP*100:.2f}% · 오후TP "
        f"{macd2_config.N1_AFTERNOON_TP*100:.2f}% · small whipsaw HOLD 포함"
    )
    _n1_rg = str(getattr(state, "n1_regime_state", "") or "")
    _n1_tp2 = getattr(state, "n1_effective_tp2", None)
    if _n1_tp2 is not None and _n1_rg:
        st.info(
            f"📐 N1 adaptive 현재 판정: **{_n1_rg}** · effective TP2 **{_n1_tp2:.1f}%** · "
            f"TP1 {float(getattr(state, 'n1_effective_tp1', 0.0) or 0.0):.1f}% · "
            f"TP1 매도비중 {float(getattr(state, 'n1_effective_tp1_ratio', 0.0) or 0.0):.0%} · "
            f"판정봉 {(getattr(state, 'n1_last_eval_bar_ts', '') or '')[11:19] or '-'}"
        )

# ── 현재 활성 전략 (상호배타 tier 중 하나) ────────────────────────────────
_ACTIVE_STRATEGY_FIELDS = (
    ("time_window_n1_filter_enabled", macd2_config.N1_3SLOT_STRATEGY_NAME),
    ("time_window_h50_filter_enabled", macd2_config.H50_3SLOT_STRATEGY_NAME),
    ("time_window_x2lite_filter_enabled", macd2_config.X2LITE_3SLOT_STRATEGY_NAME),
    ("time_window_twf_filter_enabled", macd2_config.TW_TEG_3SLOT_STRATEGY_NAME),
    ("time_window_3slot_filter_enabled", macd2_config.TW2_3SLOT_STRATEGY_NAME),
    ("time_window_teg_filter_enabled", "TEGv2"),
    ("time_window_2_filter_enabled", "TW2"),
)
_active_names = [nm for fld, nm in _ACTIVE_STRATEGY_FIELDS if bool(getattr(state, fld, False))]
if len(_active_names) == 1:
    st.success(f"✅ 현재 활성 전략: **{_active_names[0]}**")
elif not _active_names:
    st.caption("현재 활성 전략: (없음 — 기본 MACD2 동작)")
else:
    # 상호배제가 깨진 상태는 실거래 안전성 문제다 — 조용히 넘기지 않는다.
    st.error(
        f"⚠ 전략 토글이 {len(_active_names)}개 동시에 켜져 있습니다: "
        f"{', '.join(_active_names)} — 하나만 남기고 끄십시오."
    )

# ── 조기익절 필터 (TW2 3-SLOT / TW TEG 3-SLOT 공통 서브필터, 2026-09-03) ───────
# TW2 3-SLOT이 꺼지면 service.set_time_window_3slot_filter_enabled가 이 토글을
# 강제로 끈다. 위젯 key가 session_state에 남아 있으면 다음 rerun에서 체크박스가
# 여전히 True로 읽혀 켜려는 요청이 한 번 더 나가므로(그러면 서비스가
# TW2_3SLOT_REQUIRED로 거절하고 경고만 뜬다), 의존필터가 꺼진 상태에서는 위젯
# 상태도 함께 내려 UI와 실제 상태가 어긋나지 않게 한다.
# 2026-09-07: 조기익절은 TW2 3-SLOT / TW TEG 3-SLOT 공통 서브필터다 — 둘 중
# 하나라도 켜져 있으면 사용할 수 있고, 각 전략에서 독립적으로 ON/OFF 된다.
# 2026-09-12: X2-lite 는 조기익절을 **내장**한다(자동 ON, trigger 1.5 / floor 1.0).
# 그 모드에서는 이 토글을 실행경로가 아예 읽지 않으므로, 켜고 끌 수 있는 것처럼
# 보이지 않게 비활성으로 렌더한다 — 중복 적용은 구조적으로 불가능하다.
_x2lite_live = bool(getattr(state, "time_window_x2lite_filter_enabled", False)) or \
               bool(getattr(state, "time_window_h50_filter_enabled", False))
_3slot_live = bool(
    getattr(state, "time_window_3slot_filter_enabled", False)
    or getattr(state, "time_window_twf_filter_enabled", False)
)
if (not _3slot_live or _x2lite_live) and st.session_state.get("macd2_early_tp_toggle"):
    st.session_state["macd2_early_tp_toggle"] = False

_early_tp_cols = st.columns([1.4, 1.6])
with _early_tp_cols[0]:
    _early_tp_on = st.checkbox(
        "└ 조기익절 필터",
        value=bool(getattr(state, "early_tp_filter_enabled", False)),
        key="macd2_early_tp_toggle",
        disabled=(not _3slot_live) or _x2lite_live,
        help=(
            "TW2 3-SLOT / TW TEG 3-SLOT 공통 청산측 서브필터 — 진입/슬롯/T+3/TW2 veto/Trend Quality/TEGv2 게이트는 "
            "전혀 건드리지 않고, 이미 보유 중인 포지션에만 하방 보호선을 하나 더 얹습니다(매도만 가능). "
            "① 진입이 체결된 확정봉을 CHOP/TREND로 분류해 그 포지션에 고정 저장합니다(최근30분 확정 "
            "zero-cross 횟수 / EMA10-20 스프레드 확대 실패 / EMA20 기울기 진입방향 아님 / 종가-세션VWAP "
            f"부호 교차 반복, 4개 중 {macd2_config.EARLY_TP_SCORE_MIN}개 이상이면 CHOP). 보유 중에 나중에 "
            "흔들리기 시작한 포지션은 대상이 아닙니다(그 방식은 먼저 검증했고 +6% TP2 러너를 잘라 OOS에서 "
            "악화되어 기각). ② 진입시 CHOP인 포지션만, MFE(진입 후 최고 순수익)가 "
            f"+{macd2_config.EARLY_TP_TRIGGER_PCT:.1f}%에 도달하면 armed 되고, 그 뒤 완성 3분봉 종가가 "
            f"+{macd2_config.EARLY_TP_FLOOR_PCT:.1f}% 이하로 내려오면 잔량을 전량 청산합니다. "
            "③ 기존 청산이 항상 우선합니다 — TP1/TP2/오후TP(틱 즉시)와 손절/after-TP1-stop/trailing(완성봉)을 "
            "먼저 전부 평가하고, 그중 아무것도 발동하지 않았을 때만 이 필터가 판단합니다. 그래서 실효 스탑이 "
            "max(기존 활성 스탑, floor)가 되고 TP1/TP2/trailing은 그대로 살아 있습니다. "
            "2026-06-05~08-31 60거래일 TRAIN(40)/OOS(20), 임계값은 TRAIN에서 확정하고 OOS 재조정 없음: "
            "OOS 복리 34.18%→39.68%, PF 1.87→2.07, MDD 7.95%→6.47%(개선). 다만 60거래일 116거래 중 실제 "
            "발동은 5건(OOS 2건)뿐입니다 — 손실→플러스 전환 2건, +3~6% 러너 훼손 0건으로 방향은 일관되고 "
            "러너를 훼손할 수 없는 구조지만, 통계적으로 확정된 개선은 아닌 저빈도·저하방 가드로 보셔야 합니다. "
            "참고로 30분 창이 필요해 09:15 이전 진입은 구조적으로 CHOP 판정이 불가능해 TREND(=미적용)로 "
            "떨어집니다. TW2 3-SLOT과 TW TEG 3-SLOT이 둘 다 꺼지면 자동으로 함께 비활성화됩니다. 기본 OFF. "
            "(MACD2의 기존 PROFIT_LOCK 기능과는 완전히 무관한 별개 필터입니다.)"
        ),
    )
with _early_tp_cols[1]:
    if not _3slot_live:
        st.caption("조기익절 필터=OFF · TW2 3-SLOT 또는 TW TEG 3-SLOT을 켜야 사용할 수 있습니다(자동 비활성화)")
    elif bool(_early_tp_on) != bool(getattr(state, "early_tp_filter_enabled", False)):
        res = service.set_early_tp_filter_enabled(bool(_early_tp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"조기익절 필터 → {'ON' if _early_tp_on else 'OFF'}")
            st.rerun()
        else:
            st.warning(
                "조기익절 필터를 켤 수 없습니다: "
                + ("TW2 3-SLOT 또는 TW TEG 3-SLOT이 켜져 있어야 합니다."
                   if res.get("reason") == "TW2_3SLOT_REQUIRED" else str(res.get("reason") or "알 수 없는 사유"))
            )
    else:
        st.caption(
            f"조기익절 필터={'ON' if state.early_tp_filter_enabled else 'OFF'} · "
            f"트리거 MFE +{macd2_config.EARLY_TP_TRIGGER_PCT:.1f}% → 보호선 +{macd2_config.EARLY_TP_FLOOR_PCT:.1f}% · "
            f"현재 포지션 진입시 판정={'CHOP(대상)' if getattr(state, 'time_window_entry_chop', False) else 'TREND(미적용)'}"
            + (
                f" · MFE {float(getattr(state, 'early_tp_peak_net_return', 0.0) or 0.0):+.2f}%"
                if getattr(state, "time_window_position_active", False) else ""
            )
        )
        if state.early_tp_filter_enabled and getattr(state, "last_entry_chop_score", None) is not None:
            _chop_conds = getattr(state, "last_entry_chop_conditions", None) or {}
            _hit = [k for k, v in _chop_conds.items() if v]
            st.caption(
                "최근 진입 CHOP 판정: "
                f"{int(state.last_entry_chop_score)}/{len(early_take_profit.ALL_CHOP_CONDITIONS)}"
                f" (기준 {macd2_config.EARLY_TP_SCORE_MIN} 이상) · "
                f"충족={', '.join(_hit) if _hit else '-'}"
            )
        if getattr(state, "last_early_tp_armed_at", None) or getattr(state, "last_early_tp_fired_at", None):
            st.caption(
                f"최근 armed={_hhmmss(getattr(state, 'last_early_tp_armed_at', None)) or '-'} · "
                f"최근 발동={_hhmmss(getattr(state, 'last_early_tp_fired_at', None)) or '-'}"
            )

# ── C1 Peak Protection (2026-09-19) ───────────────────────────────────────
# N1 계열(X2-lite / H50) 래더 위에 얹는 **청산 전용 overlay**. 새 전략 모드가
# 아니다 — 진입/슬롯/T+3/quality/TEG/W1a/TP1/TP2/off_tp2/손절/trailing/강제청산은
# 한 줄도 바뀌지 않는다. N1 계열이 아니면 비활성으로 렌더하고 이유를 표시한다.
_c1_family_live = bool(getattr(state, "time_window_n1_filter_enabled", False))
if (not _c1_family_live) and st.session_state.get("macd2_c1_peak_protection_toggle"):
    st.session_state["macd2_c1_peak_protection_toggle"] = False

_c1_cols = st.columns([1.4, 1.6])
with _c1_cols[0]:
    _c1_on = st.checkbox(
        "└ C1 Peak Protection",
        value=bool(getattr(state, "c1_peak_protection_enabled", False)),
        key="macd2_c1_peak_protection_toggle",
        disabled=not _c1_family_live,
        help=(
            f"N1 포지션이 +{macd2_config.C1_ARM_MFE_PCT:.1f}% 이상 수익권 도달 후 "
            f"MACD gap 반전 + peak 대비 {macd2_config.C1_GIVEBACK_PCT:.1f}%p 반납 시 전량청산. "
            "청산측 overlay 이며 진입/슬롯/T+3/quality/TEG/W1a 사이징은 전혀 건드리지 않습니다. "
            f"① 보유 포지션의 MFE(틱 관측 최고 순수익)가 +{macd2_config.C1_ARM_MFE_PCT:.1f}%에 "
            "도달하면 armed 됩니다. ② 그 뒤 **완성 3분봉**에서 MACD-Signal gap 이 보유방향 "
            "반대로 부호 전환되고(레버리지 보유: gap≤0 / 인버스 보유: gap≥0) 동시에 MFE 대비 "
            f"{macd2_config.C1_GIVEBACK_PCT:.1f}%p 이상 반납했으면 잔량을 전량 청산합니다. "
            "단순 gap 축소로는 발동하지 않습니다 — 부호가 넘어가야 합니다. "
            "③ 기존 청산이 항상 우선합니다 — TP1/TP2/오후TP(틱 즉시), 손절/after-TP1-stop/"
            "trailing(완성봉), 조기익절, 강제청산, 반대신호 switch, whipsaw-watch, H50 을 "
            "먼저 전부 평가하고 그중 아무것도 발동하지 않았을 때만 C1 이 판단합니다. "
            "검증(data/validation/macd2/c1_peak_protection_20260919/README.md, 78영업일 0527~0918 N1 기준): "
            "복리 401.09%→438.63%(+37.54%p), 30일 65.12%→66.48%, PF 2.587→2.658, "
            "MDD -8.91% 동일, -Top10 +13.39, 진입집합 diff 0. 발동 7건 전부 개선(악화 0건), "
            "TP2 8% runner 9건 손상 0. WF 6분할 4승 0패 2무, bootstrap C1>N1 99.93%, "
            "위약 대비 상위 0.3~0.4%. 민감도 plateau: arm 4.5~6.5 × give 1.0~1.5 전 구간 양수. "
            "다만 등급은 PROMISING 입니다 — OOS 구간이 없고(78일 전부 인샘플), 발동이 7건뿐이며 "
            "개선 크기의 83%가 7월 4건에서 나옵니다. 30일 창 기여(+1.36%p)는 +3분 체결지연이면 "
            "0, +0.50%p 슬리피지면 -0.30 으로 얇습니다. 그래서 **기본 OFF** 입니다. "
            "**N1 모드에서만** 켤 수 있고, 다른 전략으로 바꾸면 자동으로 꺼집니다."
        ),
    )
with _c1_cols[1]:
    if not _c1_family_live:
        st.caption("C1 Peak Protection=OFF · N1 을 켜야 사용할 수 있습니다(자동 비활성화)")
    elif bool(_c1_on) != bool(getattr(state, "c1_peak_protection_enabled", False)):
        res = service.set_c1_peak_protection_enabled(bool(_c1_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"C1 Peak Protection → {'ON' if _c1_on else 'OFF'}")
            st.rerun()
        else:
            st.warning(
                "C1 Peak Protection 을 켤 수 없습니다: "
                + str(res.get("message") or res.get("reason") or "알 수 없는 사유")
            )
    else:
        st.caption(
            f"C1 Peak Protection={'ON' if getattr(state, 'c1_peak_protection_enabled', False) else 'OFF'} · "
            f"arm MFE +{macd2_config.C1_ARM_MFE_PCT:.1f}% → 반납 허용 {macd2_config.C1_GIVEBACK_PCT:.1f}%p"
            + (
                f" · 현재 MFE {float(getattr(state, 'c1_peak_net_return', 0.0) or 0.0):+.2f}%"
                f" · {'ARMED' if getattr(state, 'c1_armed', False) else '미무장'}"
                if getattr(state, "time_window_position_active", False) else ""
            )
        )
        if getattr(state, "c1_armed_at", None) or getattr(state, "c1_triggered_at", None):
            st.caption(
                f"최근 armed={_hhmmss(getattr(state, 'c1_armed_at', None)) or '-'} · "
                f"최근 발동={_hhmmss(getattr(state, 'c1_triggered_at', None)) or '-'}"
            )

# ── SMART 사이징 (2026-09-22) ─────────────────────────────────────────────
# P2 슬롯 배분 + toxic confirmation 감액을 **하나의 정책**으로 합친 단일 토글.
# 이전 P2 토글을 대체한다(별도 Toxic 토글은 만들지 않는다).
# **사이징 전용** — 진입/슬롯/T+3/quality/TEG/청산은 한 줄도 바뀌지 않고
# 이미 승인된 진입의 주문수량 배수만 바뀐다. N1 + C1 이 **둘 다** 켜져 있어야
# 켤 수 있다 — 앵커가 그 조합에서만 측정됐기 때문이다.
_sm_n1 = bool(getattr(state, "time_window_n1_filter_enabled", False))
_sm_c1 = bool(getattr(state, "c1_peak_protection_enabled", False))
_sm_ready = _sm_n1 and _sm_c1
_sm_env = bool(macd2_position_sizing.forced_by_env())
_sm_state_on = bool(getattr(state, "smart_sizing_enabled", False))
if (not _sm_ready) and st.session_state.get("macd2_smart_sizing_toggle"):
    st.session_state["macd2_smart_sizing_toggle"] = False

st.markdown("**Sizing Mode**  ·  BASE / SMART")
_sm_cols = st.columns([1.4, 1.6])
with _sm_cols[0]:
    _sm_on = st.checkbox(
        "└ SMART 사이징 (끄면 BASE)",
        value=_sm_state_on,
        key="macd2_smart_sizing_toggle",
        disabled=not _sm_ready,
        help=(
            "슬롯 기반 P2 배분 + toxic confirmation 감액을 하나로 합친 사이징입니다. "
            "**주문금액 배수만** 바꿉니다. "
            f"Slot1/2 ×{macd2_config.P2_SIZING_SLOT12_MULT:.2f} · "
            f"오전(~11:00) Slot3 ×{macd2_config.P2_SIZING_MORNING_SLOT3_MULT:.2f} · "
            f"오후 Slot3 ×{macd2_config.P2_SIZING_AFTERNOON_SLOT3_MULT:.2f} · "
            f"Toxic 은 슬롯과 무관하게 ×{macd2_config.SMART_TOXIC_MULT:.2f} 로 "
            "**덮어씁니다**(곱하지 않으므로 0.0625 같은 이중감액이 생기지 않습니다). "
            "toxic = 확인구간(플래그봉~진입 직전) 보유 ETF 수익률 ≤ 0% "
            f"AND 하이닉스 EMA20−EMA50 방향정규화 < {macd2_config.TOXIC_EMA20_50_MAX_PCT:.2f}%. "
            "확인구간 데이터가 모자라면 toxic 으로 보지 않습니다(감액 없음). "
            "진입 판정·슬롯 배분·T+3·quality·TEG·하루 3회 상한·TP1/TP2·손절·trailing·"
            "조기익절·C1·H50 은 전혀 건드리지 않습니다. 하루 원금한도도 기존 "
            "노출상한 3.00 × 1,000만원 = 3,000만원 그대로입니다. "
            "검증(research_20260922d_confirmation_path/, 78영업일 0527~0918 N1+C1): "
            "uplift +2,106,468원, PF 2.658→3.228, MDD -3.08%→-2.53%, 거래 158건 동일"
            "(진입집합/청산 diff 0), 30일 +423,935 · OOS48 +1,682,533, "
            "앞39/뒤39 둘 다 양수, 5분할 5/5, WF6 6/6, "
            "runner MFE≥5%/≥8% 손상 0건, 일예산 초과 0일. "
            "다만 등급은 **PROMISING** 이지 ADOPT 가 아닙니다 — 최근 30일 "
            "부트스트랩이 84.5%(기준 95%)이고 효과의 96%가 slot1 toxic 12건에서 "
            "나옵니다. 그래서 **기본 OFF** 입니다. "
            "**N1 + C1 이 모두 켜져 있어야** 켤 수 있고, 둘 중 하나라도 끄면 "
            "자동으로 꺼집니다."
        ),
    )
with _sm_cols[1]:
    if not _sm_ready:
        _missing = " + ".join(x for x, on in (("N1", _sm_n1), ("C1", _sm_c1)) if not on)
        st.caption(f"Sizing Mode=BASE · {_missing} 을(를) 켜야 SMART 를 쓸 수 있습니다(자동 비활성화)")
    elif bool(_sm_on) != _sm_state_on:
        _res = service.set_smart_sizing_enabled(bool(_sm_on), changed_by="ui")
        if _res.get("ok"):
            st.caption(f"Sizing Mode → {'SMART' if _sm_on else 'BASE'}")
            st.rerun()
        else:
            st.warning(
                "SMART 사이징을 켤 수 없습니다: "
                + str(_res.get("message") or _res.get("reason") or "알 수 없는 사유")
            )
    else:
        _sm_live = _sm_state_on or _sm_env
        st.caption(
            f"Sizing Mode={'SMART' if _sm_live else 'BASE'} · "
            f"slot1·2 ×{macd2_config.P2_SIZING_SLOT12_MULT:.2f} / "
            f"오전slot3 ×{macd2_config.P2_SIZING_MORNING_SLOT3_MULT:.2f} / "
            f"오후slot3 ×{macd2_config.P2_SIZING_AFTERNOON_SLOT3_MULT:.2f} / "
            f"**toxic ×{macd2_config.SMART_TOXIC_MULT:.2f}** · "
            f"하루한도 {macd2_config.DEFAULT_BUDGET * macd2_config.X2LITE_SIZING_DAILY_EXPOSURE_CAP:,.0f}원"
        )
        if _sm_env and not _sm_state_on:
            st.caption("⚠ 환경변수 MACD2_SIZING_MODE 로 강제 ON 상태입니다 — 토글로 끌 수 없습니다.")
        _sm_trace = getattr(state, "last_smart_sizing_trace", None) or {}
        if _sm_trace:
            st.caption(
                f"최근 진입 {_sm_trace.get('date') or '-'} {_sm_trace.get('symbol') or ''} · "
                f"{'TOXIC' if _sm_trace.get('toxic') else 'NORMAL'} · "
                f"적용배수 ×{float(_sm_trace.get('smart_multiplier') or 1.0):.4f} · "
                f"확인ETF {_sm_trace.get('confirmation_etf_return_pct')}% · "
                f"EMA20-50 {_sm_trace.get('ema20_50_directional_pct')}%"
            )

# ── X1 CONTEXT (2026-09-23) ───────────────────────────────────────────────
# 새 MACD 신호를 만들지 않는 **보조필터**다. 기존 RED/BLUE 신호를 입력으로 받아
# "지금 맥락에서 이 신호를 어떻게 다룰지"만 답한다. 주문금액은 계산하지 않는다 --
# SMART sizing 은 X1 이 ENTRY/REENTRY/LATE_ENTRY 를 허용한 뒤에 따로 돈다.
# SMART 와 같은 관례: N1 + C1 이 둘 다 켜져 있어야 켤 수 있다. 기본 OFF.
_x1_n1 = bool(getattr(state, "time_window_n1_filter_enabled", False))
_x1_c1 = bool(getattr(state, "c1_peak_protection_enabled", False))
_x1_ready = _x1_n1 and _x1_c1
_x1_state_on = bool(getattr(state, "x1_context_enabled", False))
_x1_shadow_on = bool(getattr(state, "x1_shadow_mode_enabled", False))
if (not _x1_ready) and st.session_state.get("macd2_x1_context_toggle"):
    st.session_state["macd2_x1_context_toggle"] = False

st.markdown("**X1 CONTEXT**  ·  OFF / ON")
_x1_cols = st.columns([1.4, 1.6])
with _x1_cols[0]:
    _x1_on = st.checkbox(
        "└ X1 CONTEXT",
        value=_x1_state_on,
        key="macd2_x1_context_toggle",
        disabled=not _x1_ready,
        help=(
            "프리마켓·당일 추세·flip sequence·오후 재진입을 종합해 진입/청산을 보정합니다. "
            "**새 MACD 신호를 만들지 않습니다** — 기존 RED/BLUE 플래그를 입력으로 받아 "
            "그 신호를 어떻게 다룰지만 판단합니다. 하위 4개 모듈:\n\n"
            "① MORNING CONTEXT — 08:00 프리마켓부터 현재까지의 흐름과 플래그 방향이 "
            "맞는지 점수화해 PASS / PASS_WEAK / WATCH 로 나눕니다. 역행 whipsaw 는 "
            "즉시 차단하지 않고 WATCH 로 보류했다가 "
            f"{macd2_config.X1_MORNING_WATCH_MAX_MIN}분 안에 원래 방향으로 breakout 이 "
            "나오면 재승인합니다.\n\n"
            "② FLIP EXIT — H50 이 whipsaw 로 HOLD 한 뒤에도 방향전환이 "
            f"{macd2_config.X1_FLIP_EXIT_MIN_FLIPS}회 이상 이어지면(플래그 개수가 아니라 "
            "전환 횟수입니다) 반대방향 breakout 시 전량청산합니다. "
            "**자동 reverse 는 하지 않습니다** — 청산과 신규진입은 별도 판단입니다.\n\n"
            "③ AFTERNOON RE-ENTRY (AR1) — 오후에 같은 방향으로 이미 한 번 거래했다는 "
            "이유만으로 버려지던 두 번째 추세를 조건부로 살립니다. TEG 탈락 조건이 "
            "price_ema_stack_aligned **하나뿐**이고 MACD gap 확대 + EMA spread 확대 + "
            "VWAP 우호가 전부 참일 때만 그 조건 하나를 면제합니다. 오후 TEG 전체를 "
            "완화하지 않습니다(새 임계값 0개).\n\n"
            "④ FLIP BREAKOUT WATCH — soft reject 된 신호를 버리지 않고 WATCH 했다가 "
            "cluster box breakout 시 late entry 후보로 살립니다. WATCH 조건은 최근 "
            f"{macd2_config.X1_FLIPWATCH_WINDOW_A_MIN}분 전환 "
            f"{macd2_config.X1_FLIPWATCH_MIN_FLIPS_A}회 이상 **또는** 최근 "
            f"{macd2_config.X1_FLIPWATCH_WINDOW_B_MIN}분 플래그 "
            f"{macd2_config.X1_FLIPWATCH_MIN_FLAGS_B}개 이상입니다. 추격 방지를 위해 지연 "
            f"{macd2_config.X1_LATE_ENTRY_MAX_LATENCY_MIN}분 이내만 허용합니다"
            "(잠정 guard, 지연 버킷은 전부 기록).\n\n"
            "**자본상한·시간창 종료·브로커/reconcile 이상·리스크 차단·포지션 충돌 같은 "
            "hard reject 는 어떤 경우에도 되살리지 않습니다.** flip sequence 는 LIVE 확정 "
            "원장 이벤트만 쓰고 재계산본은 쓰지 않습니다. 프리마켓 데이터가 없으면 "
            "fail-open — 기존 N1+C1 동작을 그대로 둡니다.\n\n"
            "**N1 + C1 이 모두 켜져 있어야** 켤 수 있고, 둘 중 하나라도 끄면 자동으로 "
            "꺼집니다. SMART 와는 독립입니다(함께 쓰는 것이 기본 연구구조). "
            "**기본 OFF** 이며, 먼저 아래 SHADOW 모드로 관찰하는 것을 권장합니다."
        ),
    )
    _x1_shadow_new = st.checkbox(
        "└ └ X1 SHADOW (주문 변경 없이 기록만)",
        value=_x1_shadow_on,
        key="macd2_x1_shadow_toggle",
        disabled=not _x1_ready,
        help=(
            "실제 주문/판정을 **전혀 바꾸지 않고** X1 이 내렸을 판정만 "
            "`x1_shadow_ledger.csv` 에 기록합니다(would_block / would_exit / "
            "would_reentry / would_late_entry). 기존 신호·거래 원장과 완전히 분리된 "
            "파일이라 flag-ledger / reconcile / manual_exit 경로에 영향이 없습니다. "
            "X1 본토글과 독립이라 shadow 만 켜고 관찰할 수 있습니다."
        ),
    )
with _x1_cols[1]:
    if not _x1_ready:
        _x1_missing = " + ".join(x for x, on in (("N1", _x1_n1), ("C1", _x1_c1)) if not on)
        st.caption(f"X1 CONTEXT=OFF · {_x1_missing} 을(를) 켜야 X1 을 쓸 수 있습니다(자동 비활성화)")
    elif bool(_x1_on) != _x1_state_on:
        _xr = service.set_x1_context_enabled(bool(_x1_on), changed_by="ui")
        if _xr.get("ok"):
            st.caption(f"X1 CONTEXT → {'ON' if _x1_on else 'OFF'}")
            st.rerun()
        else:
            st.warning(
                "X1 CONTEXT 를 켤 수 없습니다: "
                + str(_xr.get("message") or _xr.get("reason") or "알 수 없는 사유")
            )
    elif bool(_x1_shadow_new) != _x1_shadow_on:
        _xr = service.set_x1_context_enabled(
            bool(_x1_shadow_new), changed_by="ui", shadow_only=True)
        if _xr.get("ok"):
            st.caption(f"X1 SHADOW → {'ON' if _x1_shadow_new else 'OFF'}")
            st.rerun()
        else:
            st.warning(
                "X1 SHADOW 를 켤 수 없습니다: "
                + str(_xr.get("message") or _xr.get("reason") or "알 수 없는 사유")
            )
    else:
        st.caption(
            f"X1 CONTEXT={'ON' if _x1_state_on else 'OFF'}"
            f" · SHADOW={'ON' if _x1_shadow_on else 'OFF'}"
            f" · {macd2_config.X1_FILTER_VERSION}"
        )
        st.caption(
            f"flip exit 전환 {macd2_config.X1_FLIP_EXIT_MIN_FLIPS}회↑ · "
            f"late entry 점수 {macd2_config.X1_LATE_ENTRY_SCORE_MIN}↑ / 지연 "
            f"{macd2_config.X1_LATE_ENTRY_MAX_LATENCY_MIN}분 이내 · "
            f"AR1 {'ON' if macd2_config.X1_AR1_ENABLED else 'OFF'}"
        )
        _x1_trace = getattr(state, "last_x1_trace", None) or {}
        if _x1_trace:
            st.caption(
                f"X1: {_x1_trace.get('x1_final_action') or '-'}"
                f" · Premarket trend: {_x1_trace.get('premarket_trend') or '-'}"
                f" · Flip count: {_x1_trace.get('flip_count', '-')}"
                f" · Context score: {_x1_trace.get('x1_context_score', '-')}"
            )
            st.caption(f"Reason: {_x1_trace.get('x1_reasons') or '-'}")
        else:
            st.caption(
                "최근 X1 판정 없음 · X1: - / Premarket trend: - / Flip count: - / "
                "Context score: - / Reason: -"
            )



# ── 레거시 진입전략 토글 (2026-09-07 숨김) ─────────────────────────────────
# 사용자 노출 전략을 TW2 3-SLOT / TW TEG 3-SLOT 두 개로 정리하면서 감췄다.
# 코드는 그대로 두고 렌더만 막는다 -- MACD2_SHOW_LEGACY_TW2_TOGGLES=1 이면
# 아래 블록이 예전과 똑같이 다시 렌더된다(service/worker 경로는 무관하게 상시 유지).
if macd2_config.SHOW_LEGACY_TW2_TOGGLES:
    _dbe_cols = st.columns([1.4, 1.6])
    with _dbe_cols[0]:
        _dbe_on = st.checkbox(
            "+1 DOWN_BLUE",
            value=bool(getattr(state, "down_blue_exception_filter_enabled", False)),
            key="macd2_down_blue_exception_toggle",
            disabled=not bool(state.time_window_2_filter_enabled),
            help=(
                "TW2가 거절한 DOWN_BLUE 플래그 중, 다른 조건 없이 하루 최대 1회만 추가로 "
                "진입합니다. 56거래일 TRAIN/VAL/OOS 백테스트에서 조건 없이 그대로 허용하는 쪽이 세 구간 모두 일관되게 "
                "개선되어 채택됨(연쇄복리 69.3%→105.3%). 둘 다 꺼져있으면 효과 없음. 기본 OFF."
            ),
        )
    with _dbe_cols[1]:
        if bool(_dbe_on) != bool(getattr(state, "down_blue_exception_filter_enabled", False)):
            res = service.set_down_blue_exception_filter_enabled(bool(_dbe_on), changed_by="ui")
            if res.get("ok"):
                st.caption(f"+1 DOWN_BLUE → {'ON' if _dbe_on else 'OFF'}")
                st.rerun()
        else:
            st.caption(
                f"+1 DOWN_BLUE={'ON' if state.down_blue_exception_filter_enabled else 'OFF'} · "
                f"오늘 사용={'Y' if getattr(state, 'daily_down_blue_exception_used', False) else '-'}"
            )

_nf_cols = st.columns([1.4, 1.6])
with _nf_cols[0]:
    _nf_on = st.checkbox(
        "무필터 09:00-11:00 즉시청산",
        value=bool(getattr(state, "no_filter_0900_1100_enabled", False)),
        key="macd2_no_filter_0900_1100_toggle",
        help=(
            "품질점수/T+3 대기 없이 09:00-11:00에 확정 플래그가 뜨면 즉시 진입하고, 반대신호가 뜨면 "
            "항상 즉시 매도합니다(휩쏘-내성 유예 없음 — 시간대별 최적거래 필터 전용 로직). "
            "56거래일 TRAIN/VAL/OOS corrected-clock 백테스트에서 TW필터+휩쏘내성보다 우위(56일 복리 "
            "+104.8% vs +15.7%)로 확인되어 추가. 시간대별 최적거래 필터와 동시에 켜지면 그쪽이 우선합니다. 기본 OFF."
        ),
    )
with _nf_cols[1]:
    if bool(_nf_on) != bool(getattr(state, "no_filter_0900_1100_enabled", False)):
        res = service.set_no_filter_0900_1100_filter_enabled(bool(_nf_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"무필터 09:00-11:00 즉시청산 → {'ON' if _nf_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"무필터 09:00-11:00 즉시청산={'ON' if state.no_filter_0900_1100_enabled else 'OFF'}"
            + (
                " · TW2/TEG가 우선 적용됩니다"
                if state.no_filter_0900_1100_enabled and (state.time_window_teg_filter_enabled or state.time_window_2_filter_enabled)
                else ""
            )
        )

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}
bootstrap_last_result = snapshot.get("bootstrap_last_result")
today_signal_overview = snapshot.get("today_signal_overview") or []
trading_date = state.session_date or pd.Timestamp.now().strftime("%Y%m%d")
signal_rows = [r for r in ledger.load_signal_ledger(limit=2000) if r.get("trading_date") == trading_date][-100:]

st.subheader("시세 / Bootstrap 상태")
quote_status = snapshot.get("quote_status") or _quote_status(quotes)
bootstrap_status = _bootstrap_status(state, bootstrap_last_result)
w1, w2, w3, w4 = st.columns(4)
w1.metric("quote_status", quote_status)
w2.metric("bootstrap_status", bootstrap_status)
w3.metric("웜업 완료", "YES" if state.warmup_ready else "NO")
w4.metric("전략 상태", state.ui_mode.value)
if state.order_block_reason:
    st.write(f"최근 block/skip 사유: `{state.order_block_reason}`")

# ── 데이터 수집 스레드 진단 (2026-09-17, 2026-09-16 14:04 정지 사고) ──────────
# 그날 UI 에는 "스레드가 살아 있다"는 것밖에 없었고, 정작 **1분봉이 더 이상
# 들어오지 않는다**는 사실은 어디에도 표시되지 않았다. 둘은 서로 다른 문제라
# 반드시 따로 보여준다: 왼쪽 = thread alive, 오른쪽 = data fresh.
_h_alive = bool(snapshot.get("history_updater_alive"))
_h_stale_age = snapshot.get("history_stale_age_sec")
_h_success_at = snapshot.get("history_last_success_at")
_h_err = snapshot.get("history_last_error")
_h_fail_n = int(snapshot.get("history_consecutive_failures") or 0)
_h_recov_n = int(snapshot.get("history_recovery_count") or 0)
_h_wd = snapshot.get("history_watchdog") or {}
try:
    _h_success_display = (
        datetime.fromisoformat(_h_success_at).astimezone(macd2_config.KST).strftime("%H:%M:%S")
        if _h_success_at else "-"
    )
except ValueError:
    _h_success_display = "-"
_h_stale_display = f"{_h_stale_age:.0f}s" if isinstance(_h_stale_age, (int, float)) else "-"
_h_data_stale = (
    isinstance(_h_stale_age, (int, float))
    and _h_stale_age > macd2_config.HISTORY_STALE_MAX_SEC
)

st.markdown("**데이터 수집 스레드 (thread alive ≠ data fresh)**")
h1, h2, h3, h4 = st.columns(4)
h1.metric(
    "history thread", "ALIVE" if _h_alive else "DEAD",
    delta=None if _h_alive else "DEAD", delta_color="inverse" if not _h_alive else "normal",
)
h2.metric(
    "1분봉 최신 수신", _h_success_display,
    delta="STALE" if _h_data_stale else None, delta_color="inverse" if _h_data_stale else "normal",
)
h3.metric("stale age", _h_stale_display)
h4.metric("자동복구 횟수", _h_recov_n)
h5, h6, h7 = st.columns(3)
h5.metric("quote thread", "ALIVE" if snapshot.get("quote_updater_alive", quote_status != "DEAD") else "DEAD")
h6.metric("연속 fetch 실패", _h_fail_n)
h7.metric("watchdog 판정", str(_h_wd.get("verdict") or "-"))
if _h_err:
    st.warning(f"history updater 마지막 오류: `{_h_err}`")
if _h_wd.get("action") == macd2_config.HISTORY_UPDATER_RECOVERY_BLOCKED:
    st.error(
        "**history updater 복구가 막혔습니다** "
        f"(`{macd2_config.HISTORY_UPDATER_RECOVERY_BLOCKED}`) — 기존 수집 스레드의 "
        "종료가 확인되지 않아 새 스레드를 올리지 않았습니다(중복 방지). "
        "1분봉이 낡은 동안 **신규 진입은 계속 차단**되며, 기존 포지션의 손절/"
        "강제청산 경로는 그대로 동작합니다. 복구되지 않으면 프로세스를 재시작하세요."
    )
elif _h_wd.get("action") == macd2_config.HISTORY_UPDATER_START_FAILED:
    st.error(
        "**history updater 재기동에 실패했습니다** "
        f"(`{macd2_config.HISTORY_UPDATER_START_FAILED}`) — 신규 진입은 계속 차단됩니다."
    )
elif _h_data_stale or not _h_alive:
    st.warning(
        f"1분봉 수집이 정체/중단 상태입니다 (watchdog: `{_h_wd.get('verdict') or '-'}` / "
        f"`{_h_wd.get('action') or '-'}`). 신규 진입은 차단되고 기존 포지션 안전로직은 유지됩니다."
    )
# 2026-08-21 fix: order_block_reason이 POSITION_DATA_ERROR/POSITION_MISMATCH일
# 때 실제 원인(KIS 예외/응답 msg1 등)이 position_reconcile_diag에 이미 저장돼
# 있는데도 화면 어디에도 노출되지 않아, "position data error"라는 코드값만
# 보고는 진짜 원인(레이트리밋인지, 계좌 조회 실패인지)을 서버 로그 없이는 알
# 방법이 없었다 — 진단이 가장 필요한 순간에 이미 있는 정보를 숨기고 있던 셈.
_recon_diag = state.position_reconcile_diag or {}
_recon_reason = _recon_diag.get("mismatch_reason") or _recon_diag.get("broker_response_error")
if _recon_reason:
    st.write(f"포지션 조회 실패 상세: `{_recon_reason}`")

# 2026-08-20 추가: Worker._run_loop이 이미 내부적으로 추적하고 있던
# last_exception/last_tick_age_sec/stalled가 지금까지 UI 어디에도 노출되지
# 않아, "틱이 왜 멈췄는지" 진단할 방법이 대시보드에 전혀 없었다(사용자가
# 직접 코드/서버 로그를 봐야만 알 수 있었음). Worker 상태가 STALLED/DEAD가
# 아니라도(스레드 자체는 is_alive()=True로 살아있는데 내부적으로 멈춰있는
# 경우) age/exception을 그대로 보여줘 다음에 이런 상황이 재발하면 여기서
# 바로 원인을 볼 수 있게 한다.
#
# 2026-08-21 fix: 이 블록 전체가 `if worker_stats:`로 감싸여 있어, 정작
# 진단이 가장 필요한 순간(self._worker가 None인 STALLED/DEAD 상태)에는
# worker_stats가 빈 dict가 되어 화면에서 통째로 사라졌다 — "표시해달라고
# 했는데 없어졌다"는 실제 원인. 항상 렌더링하고, 데이터가 없으면 각 필드가
# 개별적으로 "-"만 보여주도록 변경한다.
last_tick_age = worker_stats.get("last_tick_age_sec")
stalled = bool(worker_stats.get("stalled"))
last_exc = worker_stats.get("last_exception")
last_tick_at_raw = worker_stats.get("last_tick_at")
try:
    last_tick_at_display = datetime.fromisoformat(last_tick_at_raw).astimezone(macd2_config.KST).strftime("%H:%M:%S") if last_tick_at_raw else "-"
except ValueError:
    last_tick_at_display = "-"
quote_fetch_times = [snap.fetched_at for snap in quotes.values() if snap is not None and snap.fetched_at]
last_quote_checked_display = max(quote_fetch_times).astimezone(macd2_config.KST).strftime("%H:%M:%S") if quote_fetch_times else "-"
d1, d2, d3 = st.columns(3)
d1.metric("마지막 tick 시간", last_tick_at_display, delta="STALLED" if stalled else None, delta_color="inverse" if stalled else "normal")
d2.metric("마지막 조회시간", last_quote_checked_display)
d3.metric("누적 tick 수", worker_stats.get("tick_n", "-"))
if last_exc:
    st.error(f"Worker 마지막 예외 (다음 성공 tick까지 유지됨):\n```\n{last_exc}\n```")

# 2026-09-03 real incident fix: state.order_block_reason이 "HISTORY_GAP" 등
# 사유 하나만 보여줘서, T+3 재확인 도중 예외가 나서 후보가 통째로 사라진
# 경우(신호원장에 아무 흔적도 안 남음)를 구분할 방법이 없었다 -- 위 "Worker
# 마지막 예외"는 다음 정상 tick이 오면 사라지는 필드라, 타이밍을 놓치면 이미
# 지나간 실패는 확인할 방법이 아예 없었다. last_resolve_error는 state에
# 영구 저장되고(재배포/재시작에도 유지) 이후 성공한 tick이 와도 자동으로
# 지워지지 않으므로, 발생 시각과 함께 항상 노출한다.
if state.last_resolve_error:
    _resolve_err_at = state.last_resolve_error_at or ""
    try:
        _resolve_err_at = datetime.fromisoformat(_resolve_err_at).astimezone(macd2_config.KST).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    st.error(f"T+3 재확인 처리 중 예외 발생 ({_resolve_err_at}, 자동으로 사라지지 않음):\n```\n{state.last_resolve_error}\n```")

st.subheader("현재 신호 / 포지션")
q1, q2, q3 = st.columns(3)
for col, symbol, label in (
    (q1, macd2_config.WATCH_SYMBOL, "SK하이닉스 000660"),
    (q2, macd2_config.LONG_SYMBOL, "KODEX 0193T0"),
    (q3, macd2_config.INVERSE_SYMBOL, "SOL 0197X0"),
):
    snap = quotes.get(symbol)
    if snap is None:
        col.metric(label, "-")
    else:
        col.metric(label, f"{snap.price:,.0f}" if snap.price else "-", delta=f"age {snap.age_sec:.1f}s" if snap.age_sec is not None else None)

s1, s2, s3 = st.columns(3)
_flag_bar_time = "-"
_latest_flag_direction = state.latest_primary_flag.value if state.latest_primary_flag else "-"
_latest_flag_signal_id = state.latest_primary_signal_id
# 2026-09-22 hotfix (실사고: 12:09 DOWN_BLUE 가 화면에 떴다가 사라짐).
# 예전에는 재계산(overview) 결과를 그대로 "마지막 플래그"로 썼다. 그런데
# compute_today_signal_overview 는 호출될 때마다 오늘 전체를 다시 걷는 순수
# 재계산이고, MACD EMA 가 누적이라 **1분봉 하나만 늦게 들어오거나 빠져도**
# 과거 플래그가 다른 시각으로 옮겨간다(repaint). 그래서 authoritative source 를
# **신호원장(워커가 실시간으로 확정·기록한 불변 이벤트)** 으로 바꾼다.
#   LIVE_CONFIRMED  원장 대응행 있음        -> 마지막 플래그 후보
#   LEDGER_ONLY     원장에만 있음(재계산에서 사라짐) -> **지우지 않고 유지**, 후보
#   RECOMPUTED_ONLY 재계산에만 있음         -> 후보 아님, 화면에 별도 표기
# 표시 전용 — 주문/진입/청산/슬롯/N1/C1/H50/사이징은 이 값을 읽지 않는다.
_flag_events = macd2_worker.reconcile_signal_overview_with_ledger(
    today_signal_overview, [row for row in signal_rows if _is_display_signal(row)]
)
_recomputed_only = [e for e in _flag_events
                    if e.get("origin") == macd2_worker.ORIGIN_RECOMPUTED_ONLY]
_ledger_only = [e for e in _flag_events
                if e.get("origin") == macd2_worker.ORIGIN_LEDGER_ONLY]
_authoritative_flag = macd2_worker.latest_ledger_backed_flag(_flag_events)
_flag_is_recomputed_only = False
if _authoritative_flag is not None:
    _latest_flag_direction = _authoritative_flag.get("direction") or _latest_flag_direction
    _latest_flag_signal_id = _authoritative_flag.get("signal_id") or _latest_flag_signal_id
    _flag_dt = _parse_flag_event_time(_authoritative_flag)
    _flag_bar_time = _flag_dt.strftime("%H:%M:%S") if _flag_dt is not None else "-"
elif _flag_events:
    # 원장 행이 하나도 없는 경우에만 재계산으로 물러나되, 그렇다고 표시한다.
    _fallback = max(_flag_events, key=lambda e: str(e.get("bar_start_at") or ""))
    _latest_flag_direction = _fallback.get("direction") or _latest_flag_direction
    _latest_flag_signal_id = _fallback.get("signal_id") or _latest_flag_signal_id
    _flag_dt = _parse_flag_event_time(_fallback)
    _flag_bar_time = _flag_dt.strftime("%H:%M:%S") if _flag_dt is not None else "-"
    _flag_is_recomputed_only = True
if _flag_bar_time == "-" and _latest_flag_signal_id:
    _parts = _latest_flag_signal_id.split("_")
    if len(_parts) >= 2 and len(_parts[1]) == 6:
        _flag_bar_time = f"{_parts[1][:2]}:{_parts[1][2:4]}:{_parts[1][4:]}"
s1.metric(
    "마지막 FLAG EVENT",
    _latest_flag_direction + (" (재계산 전용)" if _flag_is_recomputed_only else ""),
    delta=_flag_bar_time if _flag_bar_time != "-" else None,
)
if _flag_is_recomputed_only:
    s1.caption("⚠ 신호원장에 대응 행이 없습니다 — **재계산 전용 / 실시간 미기록**")
if _recomputed_only:
    s1.caption(
        "재계산 전용(실시간 미기록) "
        + ", ".join(
            f"{str(e.get('bar_start_at') or '')[11:16]} {str(e.get('direction') or '')}"
            for e in _recomputed_only[-4:]
        )
    )
if _ledger_only:
    s1.caption(
        "원장 확정(재계산에선 사라짐) "
        + ", ".join(
            f"{str(e.get('bar_start_at') or '')[11:16]} {str(e.get('direction') or '')}"
            for e in _ledger_only[-4:]
        )
    )
# 현재 MACD STATE(state.primary_relation)는 이벤트가 새로 발생했는지와 무관하게
# 매 확정봉마다 갱신되는 "지금 MACD가 Signal 위/아래 어디에 있는가"이다 — 예를
# 들어 08:45 BLUE 이벤트 이후 09:00에 새 이벤트가 없어도 이 값은 계속 BELOW로
# 남아, 화면에서 "새 이벤트 없음 = 이전 상태 유지"임을 바로 구분할 수 있다.
_state_label = {"BELOW": "BLUE 유지", "ABOVE": "RED 유지", "EQUAL": "-"}.get(state.primary_relation or "", "-")
s2.metric("현재 MACD STATE", _state_label)
if state.position:
    s3.markdown(
        f"""
        <div data-testid="metric-container" style="width:100%;">
          <label style="font-size:14px;color:rgba(49,51,63,.6);">보유 종목</label>
          <div style="font-size:20px;line-height:1.25;font-weight:600;white-space:normal;word-break:keep-all;">
            {escape(str(state.position.symbol))}<br>
            <span style="font-size:70%;">평단 {float(state.position.avg_price or 0):,.0f}</span><br>
            <span style="font-size:70%;">{int(state.position.quantity or 0):,}주 보유</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    s3.metric("보유 종목", "flat")

exec_rows_all = ledger.load_execution_ledger(limit=2000)
exec_rows = ledger.filter_execution_rows_by_trading_date(exec_rows_all, trading_date)

st.subheader("오늘의 거래 요약")


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


sell_rows_today = [r for r in exec_rows if r.get("side") == "SELL"]
# 2026-09-09 사용자 요청 (표시 전용): "왕복거래"를 매도 레그 수가 아니라 **포지션
# 단위**로 센다. TP1 50% 부분익절이 있으면 한 번의 신규진입이 SELL 레그 2개
# (TIME_WINDOW_TP1_PARTIAL + 잔량청산)를 남기므로 예전 len(sell_rows_today)는
# 진입 1회를 "2건"으로 표시했다. 집계 규칙과 그 근거는 app.ui.macd2_summary
# 모듈 docstring 에 있다 -- 원장/판정/슬롯 카운터는 읽기만 하고, 손익 지표와
# 하루 3-slot 제한에는 영향이 없다.
_has_open_position = bool(state.position)
round_trip_in_progress = macd2_summary.has_in_progress_round_trip(
    sell_rows_today, has_open_position=_has_open_position
)
round_trip_count = macd2_summary.count_round_trips(
    sell_rows_today, has_open_position=_has_open_position
)
total_gross_pnl = sum(_num(r.get("gross_pnl")) for r in exec_rows)
total_net_pnl = sum(_num(r.get("net_pnl")) for r in exec_rows)
# 세금+수수료+슬리피지를 합친 총비용 = gross와 net의 차이. 각 SELL 레그 자체의
# net_pnl은 이미 매수/매도 수수료+거래세+슬리피지를 전부 반영해 계산되므로
# (app.trading.trading_cost_engine.TradeCostEngine.compute_net_pnl), 원장의
# 개별 fee/slippage 컬럼(매도측 수수료·슬리피지만 따로 담음, 세금/청산비용은
# 컬럼에 없음)을 각각 더하는 대신 이 차이값을 쓰면 항목이 어디에 저장됐는지와
# 무관하게 항상 정확하다 -- reconcile로 발견된 진입처럼 매수 레그 자체가
# 원장에 없는 경우에도 매도(청산) 레그의 net_pnl은 이미 완전한 값이라 영향 없음.
total_cost = total_gross_pnl - total_net_pnl

sum1, sum2, sum3, sum4 = st.columns(4)
sum1.metric(
    "오늘 왕복거래 횟수",
    macd2_summary.format_round_trips(round_trip_count, in_progress=round_trip_in_progress),
)
# 왕복거래와 혼동되지 않도록 진입 슬롯을 같은 요약행에 나란히 둔다. 값은
# state.tw2_3slot_slots_used_today 를 **읽기만** 한다 -- 이 표시는 캡 판정에
# 관여하지 않으며(캡은 time_window_3slot.resolve_slot 이 같은 state 값으로
# 독립 판정), TP1 부분익절/잔량청산은 이 카운터를 증가시키지 않는다
# (worker.py 의 증가 지점은 진입 경로 2곳뿐).
sum2.metric(
    "진입 슬롯",
    f"{int(getattr(state, 'tw2_3slot_slots_used_today', 0) or 0)}"
    f"/{macd2_config.TW2_3SLOT_DAILY_CAP}",
)
sum3.metric("총 수수료+세금+슬리피지", f"{total_cost:,.0f}원")
sum4.metric("총 순수익", f"{total_net_pnl:,.0f}원")

# ── KIS 계좌 실현손익 (truth source, 2026-09-17) ────────────────────────────
# 위 "총 순수익"은 **원장 추정치**다. 2026-09-17 실거래에서 이 값이 1,756원인데
# KIS 계좌는 1,717원이었다 — 추정 수수료율(0.015%, 실제의 약 4.1배)과 체결 후
# 손익에서 또 뺀 예상 슬리피지, 그리고 평균단가 x 수량으로 계산한 체결금액
# (부분체결이 섞이면 실제와 어긋난다) 때문이었다.
#
# 아래 패널은 추정하지 않는다. KIS `inquire-daily-ccld` 의 체결금액
# (tot_ccld_amt)과 실제 제비용(prsm_tlex_smtl)을 그대로 쓴다. 이것이 계좌
# 손익의 truth 이고, 위 원장 값과 어긋나면 여기서 바로 보인다.
#
# 버튼을 눌러야 조회한다 — UI 새로고침마다 KIS 를 때리지 않기 위해서다.
st.subheader("KIS 계좌 실현손익 (실측 대조)")
st.caption(
    "계좌 화면과 **같은 API**(기간별매매손익 TTTC8715R)를 읽습니다. 추정하지 않습니다. "
    "REAL 계좌에서만 조회되며, 실패하면 당일체결(inquire-daily-ccld)로 재구성합니다."
)
_kis_cols = st.columns([1, 3])
if _kis_cols[0].button("KIS에서 조회", key="kis_realized_refresh"):
    try:
        from app.trading.kis_client import create_kis_client
        from app.trading.kis_realized import (
            account_realized_from_kis, reconcile_with_kis, verify_kis_internal_consistency,
        )

        _cli = create_kis_client(state.mode)
        if _cli is None:
            st.session_state["kis_realized"] = {"error": f"{state.mode} KIS 클라이언트를 만들 수 없습니다."}
        else:
            _today = pd.Timestamp.now(tz=macd2_config.KST).strftime("%Y%m%d")
            _pt = _cli.get_period_trade_profit(_today)
            if _pt.get("ok") and _pt.get("totals"):
                _acc = account_realized_from_kis(_pt["totals"])
                _acc["check"] = verify_kis_internal_consistency(_pt["totals"])
                st.session_state["kis_realized"] = _acc
            else:
                # fallback — 체결내역에서 재구성 (모의투자 등)
                _res = _cli.get_today_fills("")
                if not _res.get("ok"):
                    st.session_state["kis_realized"] = {
                        "error": f"{_pt.get('error') or ''} / {_res.get('error') or '조회 실패'}"}
                else:
                    _rc = reconcile_with_kis(_res.get("fills") or [], _res.get("totals") or {})
                    _s = _rc.get("summary") or {}
                    st.session_state["kis_realized"] = {
                        "source": "FALLBACK_FILLS", "buy_qty": _s.get("buy_qty"),
                        "sell_qty": _s.get("sell_qty"),
                        "buy_trade_amount": _s.get("buy_amount"),
                        "sell_trade_amount": _s.get("sell_amount"),
                        "buy_settlement_amount": None, "sell_settlement_amount": None,
                        "buy_fee": None, "sell_fee": None,
                        "fee": _s.get("fee"), "tax": _s.get("tax"),
                        "realized_pnl": _rc.get("realized_pnl"), "return_pct": None,
                        "avg_buy_price": _s.get("avg_buy_price"),
                        "avg_sell_price": _s.get("avg_sell_price"),
                        "reconcile": _rc,
                    }
    except Exception as exc:  # pragma: no cover - UI 방어
        st.session_state["kis_realized"] = {"error": f"{type(exc).__name__}: {exc}"}

_kis = st.session_state.get("kis_realized")


def _won(v):
    return "-" if v is None else f"{float(v):,.0f}원"


if not _kis:
    _kis_cols[1].caption("버튼을 누르면 KIS 계좌의 당일 실현손익을 가져와 원장과 대조합니다.")
elif _kis.get("error"):
    st.error(f"KIS 조회 실패: {_kis['error']}")
else:
    k1, k2, k3, k4 = st.columns(4)
    # 계좌 화면의 "매수금액"은 **정산금액**이다(체결금액 + 매수수수료).
    # 2026-09-17: 체결 104,375 / 정산 104,379 — 이 4원 차이가 대조 때 문제가 됐다.
    k1.metric("매수금액 (정산)", _won(_kis.get("buy_settlement_amount") or _kis.get("buy_trade_amount")),
              delta=f"체결 {_won(_kis.get('buy_trade_amount'))} · {_kis.get('buy_qty') or 0}주",
              delta_color="off")
    k2.metric("매도금액 (정산)", _won(_kis.get("sell_settlement_amount") or _kis.get("sell_trade_amount")),
              delta=f"체결 {_won(_kis.get('sell_trade_amount'))} · {_kis.get('sell_qty') or 0}주",
              delta_color="off")
    k3.metric("매매비용", _won(_kis.get("fee")),
              delta=(f"매수 {_won(_kis.get('buy_fee'))} / 매도 {_won(_kis.get('sell_fee'))}"
                     if _kis.get("buy_fee") is not None else "매수·매도 합계"),
              delta_color="off")
    k4.metric("실현손익", _won(_kis.get("realized_pnl")),
              delta=(f"{_kis['return_pct']:.4f}%" if _kis.get("return_pct") is not None else None))
    _ap, _sp = _kis.get("avg_buy_price"), _kis.get("avg_sell_price")
    st.caption(
        f"제세금 {_won(_kis.get('tax'))} · 평균 매수가 {(f'{_ap:,.2f}원' if _ap else '-')}"
        f" / 평균 매도가 {(f'{_sp:,.2f}원' if _sp else '-')} · 출처 `{_kis.get('source')}`"
    )
    st.caption(
        "**매수금액 = 체결금액 + 매수수수료**, **매도금액 = 체결금액 − 매도수수료** 입니다. "
        "실현손익은 두 방식이 같은 값입니다 — (매도체결 − 매수체결 − 총비용) = (매도정산 − 매수정산)."
    )
    _chk = _kis.get("check")
    if _chk and not _chk.get("ok"):
        st.warning(f"KIS 내부 숫자끼리 검산이 맞지 않습니다: {_chk}")
    _rc = _kis.get("reconcile")
    if _rc and not _rc.get("ok"):
        st.warning(
            "KIS 집계와 재구성 값이 어긋납니다 — 체결 누락이나 수수료 체계 변경을 의심하세요. "
            f"(체결금액 차이 {_rc.get('amount_diff')}원 / 제비용 차이 {_rc.get('fee_diff')}원)"
        )
    _diff = float(_kis.get("realized_pnl") or 0.0) - float(total_net_pnl or 0.0)
    if abs(_diff) >= 1:
        st.info(
            f"원장 추정 순수익 {total_net_pnl:,.0f}원 vs **KIS 실현손익 "
            f"{_kis.get('realized_pnl', 0):,.0f}원** — 차이 {_diff:+,.0f}원. "
            "KIS 값이 실제입니다. 원장 추정치는 전략 판정용 비용모델(수수료율 0.015% · "
            "예상 슬리피지)을 쓰고, 이 계좌에 외부(MTS 등) 주문이 섞여 있으면 그만큼도 차이로 나타납니다."
        )

st.subheader("매매 내역 (한눈에 보기)")
_trade_history = _trade_history_rows(exec_rows, signal_rows)
if _trade_history:
    st.dataframe(pd.DataFrame(_trade_history), use_container_width=True, hide_index=True)
    if any(LEGACY_FEE_MARK in str(r.get("총 수수료") or "") for r in _trade_history):
        st.caption(LEGACY_FEE_NOTE)
else:
    st.caption("오늘 기록된 매수/매도가 없습니다.")

st.subheader("신호 원장 (오늘, 최근 100건)")
if signal_rows:
    timeline_rows = _signal_timeline_rows(signal_rows)
    if timeline_rows:
        st.dataframe(pd.DataFrame(timeline_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("오늘 표시할 플래그/주문 신호가 없습니다.")
    with st.expander("신호 원장 전체 컬럼 보기 (진단용)"):
        st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
else:
    st.caption("오늘 기록된 신호가 없습니다.")

with st.expander("체결 원장 전체 컬럼 보기 (진단용, 오늘 최근 100건)"):
    if exec_rows:
        st.dataframe(pd.DataFrame(exec_rows[-100:]), use_container_width=True)
    else:
        st.caption("오늘 기록된 체결이 없습니다.")

# ── 프리마켓 Carry SHADOW (관측 전용) ─────────────────────────────────────
# 2026-09-13. **읽기만** 하는 패널이다. 여기서 주문/상태를 바꾸는 버튼은 없고,
# shadow 모듈도 주문 경로와 완전히 분리돼 있다. 백테스트 70일 재검증에서
# 08:45~08:59 봉 커버리지가 8.0%(28/350)에 불과해 표본이 3건뿐이었으므로,
# 실제 라이브 표본이 쌓이는 것을 지켜보기 위한 패널이다.
with st.expander("프리마켓 Carry SHADOW (관측 전용 · 실주문 없음)"):
    try:
        from app.trading.macd2 import premarket_shadow as _pms

        _v = _pms.today_view()
        st.caption(
            "08:45~08:59 프리마켓 MACD 플래그를 09:03에 재확인해 "
            "**가상으로만** 진입/청산했을 때의 결과를 기록합니다. "
            "실제 주문·슬롯·잔고에는 전혀 영향이 없습니다."
        )
        _c = st.columns(4)
        _c[0].metric("누적 표본", f"{_v.get('total_samples', 0)}건")
        _wr = _v.get("recent20_win_rate")
        _c[1].metric("최근20 승률", "-" if _wr is None else f"{_wr:.1f}%")
        _an = _v.get("recent20_avg_net")
        _c[2].metric("최근20 평균손익", "-" if _an is None else f"{_an:+.3f}%")
        _c[3].metric("오늘 carry", "성립" if _v.get("carry_confirmed") else "미성립")

        _rows = [
            ("오늘 날짜", _v.get("date") or "-"),
            ("마지막 프리마켓 플래그", _v.get("last_flag_time") or "-"),
            ("플래그 방향", _v.get("last_flag_direction") or "-"),
            ("09:00 상태", _v.get("state_0900") or "-"),
            ("09:03 재확인", _v.get("state_0903") or "-"),
            ("취소 사유", _v.get("cancel_reason") or "-"),
            ("가상 진입가", _v.get("shadow_entry_price") if _v.get("shadow_entry_price") else "-"),
            ("가상 사이징", _v.get("shadow_sizing") if _v.get("shadow_sizing") else "-"),
            ("가상 손익", ("-" if _v.get("shadow_net_pct") is None
                        else f"{float(_v['shadow_net_pct']):+.3f}%")),
            ("가상 청산 사유", _v.get("shadow_exit_reason") or "-"),
        ]
        st.dataframe(pd.DataFrame(_rows, columns=["항목", "값"]),
                     use_container_width=True, hide_index=True)

        _summary = _pms.read_summary(limit=60)
        if _summary:
            st.caption("일별 요약 (최근 60일)")
            st.dataframe(pd.DataFrame(_summary), use_container_width=True,
                         hide_index=True)
        else:
            st.caption("아직 기록된 프리마켓 표본이 없습니다.")

        if _v.get("total_samples", 0) < 5:
            st.info(
                f"표본 {_v.get('total_samples', 0)}건 — 70일 백테스트에서도 3건뿐이라 "
                "판정 불가였습니다. **5건 이상 쌓이기 전에는 실매매 전환 금지.**"
            )
    except Exception as _exc:   # UI 실패가 페이지 전체를 죽이지 않게
        st.caption(f"프리마켓 SHADOW 패널을 표시할 수 없습니다: {_exc}")

with st.expander("전략 설명"):
    st.markdown(
        f"""
- **신호**: SK하이닉스(000660) 3분봉 MACD({macd2_config.EMA_FAST},{macd2_config.EMA_SLOW},{macd2_config.EMA_SIGNAL}) confirmed crossover.
- **방향→매수**: RED → {macd2_config.LONG_SYMBOL}(레버리지), BLUE → {macd2_config.INVERSE_SYMBOL}(인버스).
- **반대 플래그**: 보유 포지션 전량매도 후 반대 ETF 매수(entry_gate 통과 시에만 재매수, 매도는 항상 실행).
- **리스크**: {macd2_config.FORCE_LIQUIDATE_AT} 강제청산 — 매 tick마다 플래그 발생 여부와 무관하게 확인.
- **퀵 Profit 익절(옵션, 기본 OFF)**: ON이면 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절 — 확정 플래그와 무관하게 매 tick 확인.
- **시간대별 최적거래 필터 TW2(기본 ON)**: 이 필터가 진입권한 + 포지션 관리(TP1/TP2/손절 래더)를 모두 담당 — 완성봉 플래그 확정 후 다음 완성 3분봉(T+3)에서 재확인해야만 진입, VWAP 역행 veto/최근30분 교차과다 veto 추가, TP2 5%→6%.
- **+TEGv2(옵션, 기본 OFF)**: 기존 TW2 하루 3회 진입한도를 모두 소진한 뒤 REJECT_MAX_ENTRY_COUNT 때문에만 막힌 후보에 한해 하루 1회 TEGv2 검증 통과 시 추가 진입.
- **+1 DOWN_BLUE(옵션, 기본 OFF)**: ON이면 TW2가 거절한 DOWN_BLUE 플래그 중 하루 최대 1회만 다른 조건 없이 추가로 진입.
- **09:03 예약 매수(옵션)**: 개장 직후 데이터 부족으로 이른 플래그를 놓치는 문제 대응 — 미리 예약해두면 09:03에 지정 방향 ETF를 자동으로 전량매수(하루 1회).
        """
    )
