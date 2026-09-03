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

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.macd2 import config as macd2_config  # noqa: E402
from app.trading.macd2 import early_take_profit  # noqa: E402
from app.trading.macd2 import ledger  # noqa: E402
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


def _latest_flag_event(rows: list[dict]) -> dict | None:
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


def _trade_history_rows(exec_rows: list[dict], signal_rows: list[dict]) -> list[dict]:
    """하나의 "실제 주문/TP 단계"(같은 symbol/side/경제적 이벤트, 2분 이내
    체결)를 한 행으로 합쳐 총 체결수량/수량가중평균가/총 수수료/총 net_pnl/
    최초~최종 체결시각을 보여준다 -- 원본 execution 원장은 절대 건드리지
    않고(_aggregate_trade_legs는 읽기 전용 그룹핑), 화면 표시만 집계한다.
    TP1 partial과 최종청산처럼 실제로 서로 다른 매도 의사결정이면 언제나
    별도 그룹(별도 행)으로 남는다 (_economic_bucket 참고). signal_id로 신호
    원장과 매칭해 진입 방향(레드/블루)을 가져온다.

    2026-08-31 사용자 요청: RECOVERED_QTY_MISMATCH/RECONCILE_BACKFILL/
    BROKER_DIRECT 등 정합화 전용 행이 실제 주문과 2분 이내로 붙지 못해
    "고아" 그룹(group["placeholder"] == True -- 진짜 경제적 결정 행이 단
    하나도 합류하지 못한 그룹)이 되는 경우, 메인 표에는 아예 노출하지
    않는다 -- 여전히 진단용 "체결 원장 전체 컬럼 보기" expander에는 원본
    그대로 남아 있으므로 정보 자체가 사라지는 것은 아니다."""
    direction_by_signal_id = {
        r.get("signal_id"): r.get("direction") for r in signal_rows if r.get("signal_id")
    }
    rows: list[dict] = []
    for group in _aggregate_trade_legs(exec_rows):
        if group["placeholder"]:
            continue  # pure reconcile/BROKER_DIRECT group, never a real decision -- hide from the main table
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
                "사유": _direction_display(direction),
                "총 순이익": "-" if not any(r.get("net_pnl") for r in legs) else _money_display(total_net_pnl),
                "총 수수료": _money_display(total_fee) if total_fee else "-",
            })
        elif side == "SELL":
            reason_label = _exit_reason_display(group["bucket"]) if not group["placeholder"] else "정합화"
            rows.append({
                "일시": when, "종목": symbol, "매수/매도": "매도",
                "총 체결수량": _qty_display(total_qty), "체결가(수량가중평균)": _price_display(weighted_price),
                "사유": reason_label,
                "총 순이익": _money_display(total_net_pnl),
                "총 수수료": _money_display(total_fee),
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

# ── 조기익절 필터 (TW2 3-SLOT 전용 서브필터, 2026-09-03) ────────────────────
# TW2 3-SLOT이 꺼지면 service.set_time_window_3slot_filter_enabled가 이 토글을
# 강제로 끈다. 위젯 key가 session_state에 남아 있으면 다음 rerun에서 체크박스가
# 여전히 True로 읽혀 켜려는 요청이 한 번 더 나가므로(그러면 서비스가
# TW2_3SLOT_REQUIRED로 거절하고 경고만 뜬다), 의존필터가 꺼진 상태에서는 위젯
# 상태도 함께 내려 UI와 실제 상태가 어긋나지 않게 한다.
_3slot_live = bool(getattr(state, "time_window_3slot_filter_enabled", False))
if not _3slot_live and st.session_state.get("macd2_early_tp_toggle"):
    st.session_state["macd2_early_tp_toggle"] = False

_early_tp_cols = st.columns([1.4, 1.6])
with _early_tp_cols[0]:
    _early_tp_on = st.checkbox(
        "└ 조기익절 필터",
        value=bool(getattr(state, "early_tp_filter_enabled", False)),
        key="macd2_early_tp_toggle",
        disabled=not _3slot_live,
        help=(
            "TW2 3-SLOT 전용 청산측 서브필터 — 진입/슬롯/T+3/TW2 veto/Trend Quality/TEGv2 게이트는 "
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
            "떨어집니다. TW2 3-SLOT이 꺼지면 자동으로 함께 비활성화됩니다. 기본 OFF. "
            "(MACD2의 기존 PROFIT_LOCK 기능과는 완전히 무관한 별개 필터입니다.)"
        ),
    )
with _early_tp_cols[1]:
    if not _3slot_live:
        st.caption("조기익절 필터=OFF · TW2 3-SLOT을 켜야 사용할 수 있습니다(자동 비활성화)")
    elif bool(_early_tp_on) != bool(getattr(state, "early_tp_filter_enabled", False)):
        res = service.set_early_tp_filter_enabled(bool(_early_tp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"조기익절 필터 → {'ON' if _early_tp_on else 'OFF'}")
            st.rerun()
        else:
            st.warning(
                "조기익절 필터를 켤 수 없습니다: "
                + ("TW2 3-SLOT이 켜져 있어야 합니다."
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
_latest_event_rows = list(today_signal_overview) + [row for row in signal_rows if _is_display_signal(row)]
if _latest_event_rows:
    _latest_overview_flag = _latest_flag_event(_latest_event_rows)
    if _latest_overview_flag is not None:
        _latest_flag_direction = _latest_overview_flag.get("direction") or _latest_flag_direction
        _latest_flag_signal_id = _latest_overview_flag.get("signal_id") or _latest_flag_signal_id
        _flag_dt = _parse_flag_event_time(_latest_overview_flag)
        _flag_bar_time = _flag_dt.strftime("%H:%M:%S") if _flag_dt is not None else "-"
if _flag_bar_time == "-" and _latest_flag_signal_id:
    _parts = _latest_flag_signal_id.split("_")
    if len(_parts) >= 2 and len(_parts[1]) == 6:
        _flag_bar_time = f"{_parts[1][:2]}:{_parts[1][2:4]}:{_parts[1][4:]}"
s1.metric(
    "마지막 FLAG EVENT",
    _latest_flag_direction,
    delta=_flag_bar_time if _flag_bar_time != "-" else None,
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
round_trip_count = len(sell_rows_today)
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

sum1, sum2, sum3 = st.columns(3)
sum1.metric("오늘 왕복거래 횟수", f"{round_trip_count}건")
sum2.metric("총 수수료+세금+슬리피지", f"{total_cost:,.0f}원")
sum3.metric("총 순수익", f"{total_net_pnl:,.0f}원")

st.subheader("매매 내역 (한눈에 보기)")
_trade_history = _trade_history_rows(exec_rows, signal_rows)
if _trade_history:
    st.dataframe(pd.DataFrame(_trade_history), use_container_width=True, hide_index=True)
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
