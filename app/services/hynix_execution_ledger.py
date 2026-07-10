"""hynix_execution_ledger.py — 하이닉스⇄인버스 자동매매의 단일 거래 원장(Source of Truth).

기존에는 매수/스위칭/강제청산이 `hynix_auto_trade_log_*.csv`에, Dynamic Exit AI의
청산이 `exit_engine_log.csv`에 각각 따로 기록되어(레거시 TP/SL과 강제청산도 별도
필드 구성) "하나의 CSV만 보면 절반만 보이는" 문제가 있었다(2026-07-10 실측 — UI의
"최근 매도 8,685원"이 exit_engine_log에만 있고 export CSV에는 없었던 사고).

이 모듈은 모든 주문 실행 경로(신규진입/스위칭/레거시 TP·SL/강제청산/Dynamic Exit AI)가
공통으로 거치는 `app.trading.hynix_switch_position_manager._record_order()`에서
호출되는 단일 기록 지점이다. UI의 오늘 거래횟수/실현손익/최근 매수·매도가는 반드시
이 원장 + broker.get_positions()만으로 계산해야 한다(여러 CSV/state 필드를 따로
집계하지 말 것).
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_LEDGER_PATH = ROOT / "data" / "logs" / "hynix_execution_ledger.csv"

# 레거시(원장 도입 이전) 로그 — 백필 전용으로만 읽는다.
_LEGACY_TRADE_LOG_PATH = ROOT / "data" / "logs" / "hynix_auto_trade_log_{date}.csv"
_LEGACY_EXIT_LOG_PATH = ROOT / "data" / "logs" / "exit_engine_log.csv"

LEDGER_COLUMNS = [
    "trade_id", "parent_trade_id", "timestamp", "mode", "environment", "strategy_name",
    "signal_source", "action", "symbol", "requested_qty", "executed_qty", "requested_price",
    "executed_price", "before_qty", "after_qty", "cash_before", "cash_after", "realized_pnl",
    "fees", "tax", "success", "order_id", "position_confirmed", "is_test_order",
]

SIGNAL_SOURCE_ENHANCED_LEGACY = "ENHANCED_LEGACY"
SIGNAL_SOURCE_PREDICTION_V2 = "PREDICTION_V2"
SIGNAL_SOURCE_CYCLE_AI = "CYCLE_AI"
SIGNAL_SOURCE_DYNAMIC_EXIT = "DYNAMIC_EXIT"
SIGNAL_SOURCE_FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
SIGNAL_SOURCE_TEST = "TEST"


def _new_trade_id() -> str:
    return uuid.uuid4().hex[:16]


def record_execution(
    action: str, symbol: str, requested_qty: int, executed_qty: int,
    requested_price: Optional[float], executed_price: Optional[float],
    success: bool, mode: str = "mock", environment: Optional[str] = None,
    strategy_name: str = "hynix_switch", signal_source: str = SIGNAL_SOURCE_ENHANCED_LEGACY,
    before_qty: Optional[int] = None, after_qty: Optional[int] = None,
    cash_before: Optional[float] = None, cash_after: Optional[float] = None,
    realized_pnl: Optional[float] = None, fees: float = 0.0, tax: float = 0.0,
    order_id: str = "", position_confirmed: Optional[bool] = None,
    is_test_order: bool = False, parent_trade_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    """단일 체결/시도를 원장에 append하고 새로 발급한 trade_id를 반환한다.

    실패한 시도(success=False)도 기록한다 — "왜 이번엔 주문이 안 나갔는지" 추적을
    위해 스킵/실패 사유까지 원장에 남기는 것이 목적이다.
    """
    trade_id = _new_trade_id()
    now = now or datetime.now()
    row = {
        "trade_id": trade_id, "parent_trade_id": parent_trade_id or "",
        "timestamp": now.isoformat(timespec="seconds"), "mode": mode,
        "environment": environment or ("REAL" if mode == "real" else "MOCK"),
        "strategy_name": strategy_name, "signal_source": signal_source,
        "action": action, "symbol": symbol,
        "requested_qty": requested_qty, "executed_qty": executed_qty,
        "requested_price": requested_price, "executed_price": executed_price,
        "before_qty": before_qty, "after_qty": after_qty,
        "cash_before": cash_before, "cash_after": cash_after,
        "realized_pnl": realized_pnl, "fees": fees, "tax": tax,
        "success": bool(success), "order_id": order_id or "",
        "position_confirmed": position_confirmed, "is_test_order": bool(is_test_order),
    }
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _LEDGER_PATH.exists()
        with _LEDGER_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.error("[ExecutionLedger] 원장 기록 실패(중대 — 손익/거래횟수 집계에 영향): %s", exc)
    return trade_id


def load_ledger(date_str: Optional[str] = None) -> pd.DataFrame:
    """원장 CSV를 읽어(date_str이 주어지면 그 날짜만) DataFrame으로 반환한다."""
    if not _LEDGER_PATH.exists():
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    try:
        df = pd.read_csv(_LEDGER_PATH)
    except Exception as exc:
        logger.error("[ExecutionLedger] 원장 로드 실패: %s", exc)
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    if date_str:
        df = df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
    return df.sort_values("timestamp").reset_index(drop=True)


def compute_trade_counters(date_str: Optional[str] = None, include_test: bool = False) -> dict:
    """오늘 주문체결/매수체결/매도체결/왕복거래/테스트거래/운영거래 건수를 원장 기준으로 계산한다.

    "왕복거래"는 포지션이 0 → 보유 → 0으로 돌아온 횟수(심볼 무관, 순서대로 카운트)로 정의한다.
    """
    df = load_ledger(date_str)
    if df.empty:
        return {
            "order_fill_count": 0, "buy_fill_count": 0, "sell_fill_count": 0,
            "round_trip_count": 0, "test_order_count": 0, "live_order_count": 0,
        }
    filled = df[df["success"] == True]  # noqa: E712
    if not include_test:
        live = filled[filled["is_test_order"] != True]  # noqa: E712
    else:
        live = filled
    test_count = int((filled["is_test_order"] == True).sum())  # noqa: E712

    buy_count = int((live["action"] == "BUY").sum())
    sell_count = int((live["action"] == "SELL").sum())

    # 왕복거래: after_qty가 0으로 떨어질 때마다 1회의 "청산 완료"로 카운트한다
    # (해당 시점까지 최소 1건의 BUY가 선행했다는 전제 — after_qty 컬럼이 없는 과거
    # 백필 데이터는 셀 수 없으므로 0 처리).
    round_trips = 0
    if "after_qty" in live.columns:
        closes = live[(live["action"] == "SELL") & (pd.to_numeric(live["after_qty"], errors="coerce") == 0)]
        round_trips = int(len(closes))

    return {
        "order_fill_count": int(len(live)), "buy_fill_count": buy_count, "sell_fill_count": sell_count,
        "round_trip_count": round_trips, "test_order_count": test_count,
        "live_order_count": int(len(live)),
    }


def compute_realized_pnl_breakdown(date_str: Optional[str] = None) -> dict:
    """원장의 realized_pnl 합계 + 거래별 상세 내역을 반환한다(section 12: 손익 재구성)."""
    df = load_ledger(date_str)
    if df.empty:
        return {"total_realized_pnl": 0.0, "trades": []}
    live = df[(df["success"] == True) & (df["is_test_order"] != True)]  # noqa: E712
    live = live[live["action"] == "SELL"].copy()
    live["realized_pnl"] = pd.to_numeric(live["realized_pnl"], errors="coerce").fillna(0.0)
    trades = live[[
        "trade_id", "timestamp", "symbol", "executed_qty", "executed_price", "realized_pnl", "signal_source",
    ]].to_dict("records")
    return {"total_realized_pnl": round(float(live["realized_pnl"].sum()), 2), "trades": trades}


def compute_performance_stats(date_str: Optional[str] = None) -> dict:
    """명세 13절 — 운영 UI 통계(왕복거래 수/체결 수/평균 진입비중/평균 보유시간/
    평균 거래수익률/승률/Profit Factor/최대 장중 손실/전략별·BUY-INVERSE별·
    시험진입/Scale-in별 손익). TEST 주문은 전부 제외한다."""
    df = load_ledger(date_str)
    empty = {
        "round_trip_count": 0, "order_fill_count": 0, "avg_entry_pct": None, "avg_holding_minutes": None,
        "avg_trade_return_pct": None, "win_rate": None, "profit_factor": None, "max_intraday_drawdown_krw": None,
        "cumulative_realized_pnl": 0.0, "pnl_by_signal_source": {}, "pnl_by_symbol": {},
        "test_entry_pnl": 0.0, "scale_in_pnl": 0.0,
    }
    if df.empty:
        return empty

    live = df[(df["success"] == True) & (df["is_test_order"] != True)].copy()  # noqa: E712
    if live.empty:
        return empty
    live["realized_pnl"] = pd.to_numeric(live["realized_pnl"], errors="coerce")

    sells = live[live["action"] == "SELL"].copy()
    pnl_values = sells["realized_pnl"].dropna()
    wins = pnl_values[pnl_values > 0]
    losses = pnl_values[pnl_values < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    win_rate = round(len(wins) / len(pnl_values) * 100.0, 2) if len(pnl_values) > 0 else None

    counters = compute_trade_counters(date_str)

    avg_entry_pct = None  # requested_qty*executed_price 대비 예산비중은 호출부(브로커 예산)가 있어야 정확 — 원장만으로는 근사 생략

    # 평균 보유시간: 같은 심볼의 연속된 BUY→SELL 쌍을 순서대로 매칭(간단 FIFO 근사).
    holding_minutes: list = []
    open_time = {}
    for _, row in live.sort_values("timestamp").iterrows():
        sym = row["symbol"]
        if row["action"] == "BUY" and sym not in open_time:
            open_time[sym] = row["timestamp"]
        elif row["action"] == "SELL" and sym in open_time:
            delta = (row["timestamp"] - open_time[sym]).total_seconds() / 60.0
            holding_minutes.append(delta)
            after_qty = pd.to_numeric(row.get("after_qty"), errors="coerce")
            if pd.notna(after_qty) and after_qty <= 0:
                del open_time[sym]

    pnl_by_source = sells.groupby("signal_source")["realized_pnl"].sum().round(2).to_dict()
    pnl_by_symbol = sells.groupby("symbol")["realized_pnl"].sum().round(2).to_dict()

    cum = pd.to_numeric(sells.sort_values("timestamp")["realized_pnl"], errors="coerce").fillna(0.0).cumsum()
    running_peak = cum.cummax()
    drawdown = (cum - running_peak)
    max_dd = round(float(drawdown.min()), 2) if not drawdown.empty else 0.0

    return {
        "round_trip_count": counters["round_trip_count"], "order_fill_count": counters["live_order_count"],
        "avg_entry_pct": avg_entry_pct,
        "avg_holding_minutes": round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else None,
        "avg_trade_return_pct": None,
        "win_rate": win_rate, "profit_factor": profit_factor, "max_intraday_drawdown_krw": max_dd,
        "cumulative_realized_pnl": round(float(pnl_values.sum()), 2) if len(pnl_values) else 0.0,
        "pnl_by_signal_source": pnl_by_source, "pnl_by_symbol": pnl_by_symbol,
        "test_entry_pnl": 0.0, "scale_in_pnl": round(float(pnl_by_source.get("ACTIVE_STRATEGY_MOCK", 0.0)), 2),
    }


def reconcile_execution_ledger(date_str: Optional[str] = None, broker=None) -> dict:
    """원장과 broker.get_positions()를 대조해 UI 표시값과의 불일치를 점검한다.

    Returns
    -------
    dict: counters, pnl, broker_position, ledger_final_position, position_match(bool),
          mismatches(list[str]) — 1개라도 있으면 UI에 빨간 경고를 표시해야 한다.
    """
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    counters = compute_trade_counters(date_str)
    pnl = compute_realized_pnl_breakdown(date_str)

    df = load_ledger(date_str)
    live = df[(df["success"] == True) & (df["is_test_order"] != True)] if not df.empty else df  # noqa: E712

    ledger_position = {"symbol": None, "quantity": 0}
    if not live.empty:
        last_by_symbol = live.groupby("symbol").tail(1)
        for _, row in last_by_symbol.iterrows():
            after_qty = pd.to_numeric(row.get("after_qty"), errors="coerce")
            if pd.notna(after_qty) and after_qty > 0:
                ledger_position = {"symbol": row["symbol"], "quantity": int(after_qty)}

    mismatches: list = []
    broker_position = None
    if broker is not None:
        try:
            positions = broker.get_positions()
            broker_position = positions[0] if positions else {"symbol": None, "quantity": 0}
        except Exception as exc:
            mismatches.append(f"broker.get_positions() 조회 실패: {exc}")

    position_match = True
    if broker_position is not None:
        b_symbol = broker_position.get("symbol") if isinstance(broker_position, dict) else getattr(broker_position, "symbol", None)
        b_qty = (broker_position.get("quantity") if isinstance(broker_position, dict) else getattr(broker_position, "quantity", 0)) or 0
        if b_symbol != ledger_position["symbol"] or int(b_qty) != ledger_position["quantity"]:
            position_match = False
            mismatches.append(
                f"포지션 불일치: 원장={ledger_position}, broker={{'symbol': {b_symbol}, 'quantity': {b_qty}}}"
            )

    return {
        "date": date_str, "counters": counters, "pnl": pnl,
        "ledger_final_position": ledger_position, "broker_position": broker_position,
        "position_match": position_match, "mismatches": mismatches,
    }


# =============================================================================
# 레거시 로그 백필 (원장 도입 이전 거래를 1회성으로 재구성)
# =============================================================================

def backfill_from_legacy_logs(date_str: str, initial_cash: float = 10_000_000.0) -> dict:
    """원장 도입 이전 날짜의 hynix_auto_trade_log_*.csv + exit_engine_log.csv를 시간순으로
    합쳐 원장 형식으로 재구성한다. 이미 그 날짜 원장 데이터가 있으면 건너뛴다(중복 방지).

    E2E forced 테스트 행(reason에 "E2E forced" 포함)은 is_test_order=True로 표시하고
    운영 거래횟수/손익 계산에서 자동 제외된다(compute_* 함수들이 필터링).
    """
    existing = load_ledger(date_str)
    if not existing.empty:
        return {"skipped": True, "reason": f"{date_str} 원장에 이미 {len(existing)}건 존재 — 백필 생략"}

    rows: list = []

    trade_log_path = Path(str(_LEGACY_TRADE_LOG_PATH).format(date=date_str))
    if trade_log_path.exists():
        try:
            df = pd.read_csv(trade_log_path)
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"])
            for _, r in df.iterrows():
                reason = str(r.get("reason", ""))
                is_test = "E2E forced" in reason
                signal_source = SIGNAL_SOURCE_TEST if is_test else (
                    SIGNAL_SOURCE_FORCED_LIQUIDATION if "강제청산" in reason else SIGNAL_SOURCE_ENHANCED_LEGACY
                )
                rows.append({
                    "timestamp": r["timestamp"], "action": r.get("action"), "symbol": str(r.get("symbol")),
                    "qty": int(r.get("quantity") or 0), "price": float(r.get("price") or 0),
                    "success": bool(r.get("success")), "reason": reason,
                    "signal_source": signal_source, "is_test_order": is_test, "source_file": "hynix_auto_trade_log",
                })
        except Exception as exc:
            logger.error("[ExecutionLedger] 레거시 trade log 백필 실패(%s): %s", trade_log_path, exc)

    if _LEGACY_EXIT_LOG_PATH.exists():
        try:
            df = pd.read_csv(_LEGACY_EXIT_LOG_PATH)
            df.columns = [c.strip() for c in df.columns]
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
            for _, r in df.iterrows():
                action = r.get("action")
                if action not in ("SELL_ALL", "SELL_PARTIAL"):
                    continue
                rows.append({
                    "timestamp": r["timestamp"], "action": "SELL", "symbol": str(r.get("symbol")),
                    "qty": None, "price": float(r.get("current_price") or 0),
                    "success": True, "reason": str(r.get("reason", "")),
                    "signal_source": SIGNAL_SOURCE_DYNAMIC_EXIT, "is_test_order": False,
                    "source_file": "exit_engine_log", "entry_price": r.get("entry_price"),
                })
        except Exception as exc:
            logger.error("[ExecutionLedger] 레거시 exit log 백필 실패: %s", exc)

    if not rows:
        return {"skipped": True, "reason": "백필할 레거시 로그 데이터 없음"}

    rows.sort(key=lambda r: r["timestamp"])

    # ── 시간순으로 재생하며 포지션/현금/실현손익을 재구성한다 ──────────────
    cash = initial_cash
    position_qty = 0
    position_symbol = None
    entry_price = None
    written = 0

    for r in rows:
        action = r["action"]
        symbol = r["symbol"]
        qty = r.get("qty")
        price = r["price"]

        if action == "BUY":
            if qty is None or qty <= 0 or not price:
                continue
            before_qty = position_qty if position_symbol == symbol else 0
            cash_before = cash
            cash -= qty * price
            if position_symbol != symbol:
                position_symbol, position_qty, entry_price = symbol, qty, price
            else:
                total_cost = (entry_price or price) * position_qty + price * qty
                position_qty += qty
                entry_price = total_cost / position_qty if position_qty else price
            record_execution(
                action="BUY", symbol=symbol, requested_qty=qty, executed_qty=qty if r["success"] else 0,
                requested_price=price, executed_price=price if r["success"] else None,
                success=r["success"], mode="mock", strategy_name="hynix_switch",
                signal_source=r["signal_source"], before_qty=before_qty,
                after_qty=position_qty if r["success"] else before_qty,
                cash_before=cash_before, cash_after=cash, is_test_order=r["is_test_order"],
                now=r["timestamp"].to_pydatetime(),
            )
            written += 1

        elif action == "SELL":
            if position_symbol != symbol or position_qty <= 0:
                continue
            sell_qty = qty if qty is not None else position_qty  # exit_engine_log는 수량 없음 → 전량으로 간주
            sell_qty = min(sell_qty, position_qty)
            before_qty = position_qty
            realized = (price - (entry_price or price)) * sell_qty
            cash_before = cash
            cash += sell_qty * price
            position_qty -= sell_qty
            if position_qty <= 0:
                position_qty, position_symbol, entry_price = 0, None, None
            record_execution(
                action="SELL", symbol=symbol, requested_qty=sell_qty, executed_qty=sell_qty if r["success"] else 0,
                requested_price=price, executed_price=price if r["success"] else None,
                success=r["success"], mode="mock", strategy_name="hynix_switch",
                signal_source=r["signal_source"], before_qty=before_qty, after_qty=position_qty,
                cash_before=cash_before, cash_after=cash, realized_pnl=round(realized, 2) if r["success"] else None,
                is_test_order=r["is_test_order"], now=r["timestamp"].to_pydatetime(),
            )
            written += 1

    return {
        "skipped": False, "rows_written": written, "final_cash": round(cash, 2),
        "final_position": {"symbol": position_symbol, "quantity": position_qty},
    }
