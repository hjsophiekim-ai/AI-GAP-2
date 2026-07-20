"""hynix_strategy_shadow_tracker.py — 섹션 12/18: ACTIVE_ONLY/PREDICTION_V2_ONLY/
ADAPTIVE_FUSION/CYCLE_ONLY 4개 전략을 동일 시장 데이터로 병행 시뮬레이션해 손익을
독립적으로 집계한다.

실제로는 ADAPTIVE_FUSION(또는 ACTIVE_FUSION, 토글에 따라) 하나만 실제 공통 주문을
실행한다 — 나머지 전략은 "이 전략의 신호대로만 거래했다면 어땠을지"를 가상 포트폴리오
(Virtual Portfolio)로 시뮬레이션한다. 000660/0197X0 둘 다 그 자체로 매수·매도 가능한
종목이므로(0197X0 자체가 인버스 ETN), 부호 반전 없이 일반적인 롱 포지션 손익 계산을
그대로 적용한다.

이 모듈은 실제 브로커를 호출하지 않는다 — 순수 시뮬레이션 + 로그 기록만 수행한다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.utils.data_paths import LOGS_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
_SHADOW_LEDGER_PATH = LOGS_DIR / "hynix_strategy_shadow_ledger.csv"

STRATEGY_ACTIVE_ONLY = "ACTIVE_ONLY"
STRATEGY_PREDICTION_V2_ONLY = "PREDICTION_V2_ONLY"
STRATEGY_ADAPTIVE_FUSION = "ADAPTIVE_FUSION"
STRATEGY_CYCLE_ONLY = "CYCLE_ONLY"
ALL_STRATEGIES = [STRATEGY_ACTIVE_ONLY, STRATEGY_PREDICTION_V2_ONLY, STRATEGY_ADAPTIVE_FUSION, STRATEGY_CYCLE_ONLY]

_SHADOW_LEDGER_COLUMNS = [
    "strategy_name", "symbol", "entry_time", "exit_time", "entry_price", "exit_price",
    "quantity", "pnl_krw", "pnl_pct", "mfe_pct", "mae_pct", "holding_minutes",
]

_SYMBOL_HYNIX = "000660"
_SYMBOL_INVERSE = "0197X0"


def default_virtual_portfolio(budget: float = 10_000_000.0) -> dict:
    return {
        "_state_date": None, "cash": budget, "symbol": None, "quantity": 0,
        "entry_price": None, "entry_time": None, "mfe_pct": 0.0, "mae_pct": 0.0,
    }


def _reset_if_new_day(vp: Optional[dict], now: datetime, budget: float) -> dict:
    today = now.strftime("%Y%m%d")
    if not vp or vp.get("_state_date") != today:
        fresh = default_virtual_portfolio(budget)
        fresh["_state_date"] = today
        return fresh
    return dict(vp)


def _target_symbol_from_action(action: Optional[str]) -> Optional[str]:
    if action in ("HYNIX", "HYNIX_BUY", "ENTER_HYNIX"):
        return _SYMBOL_HYNIX
    if action in ("INVERSE", "INVERSE_BUY", "ENTER_INVERSE"):
        return _SYMBOL_INVERSE
    return None


def _append_shadow_row(row: dict) -> None:
    try:
        _SHADOW_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _SHADOW_LEDGER_PATH.exists()
        with _SHADOW_LEDGER_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=_SHADOW_LEDGER_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({c: row.get(c, "") for c in _SHADOW_LEDGER_COLUMNS})
    except Exception as exc:
        logger.debug("[StrategyShadowTracker] 기록 실패: %s", exc)


def _close_virtual_position(vp: dict, price: float, now: datetime, strategy_name: str) -> dict:
    qty = vp["quantity"]
    entry_price = vp["entry_price"]
    pnl = (price - entry_price) * qty
    pnl_pct = (price / entry_price - 1.0) * 100.0 if entry_price else 0.0
    entry_time = vp.get("entry_time")
    holding_minutes = None
    if entry_time:
        try:
            holding_minutes = round((now - datetime.fromisoformat(entry_time)).total_seconds() / 60.0, 1)
        except Exception:
            holding_minutes = None

    _append_shadow_row({
        "strategy_name": strategy_name, "symbol": vp["symbol"], "entry_time": entry_time,
        "exit_time": now.isoformat(), "entry_price": entry_price, "exit_price": price,
        "quantity": qty, "pnl_krw": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
        "mfe_pct": round(vp.get("mfe_pct", 0.0), 4), "mae_pct": round(vp.get("mae_pct", 0.0), 4),
        "holding_minutes": holding_minutes,
    })

    vp["cash"] += qty * price
    vp["symbol"] = None
    vp["quantity"] = 0
    vp["entry_price"] = None
    vp["entry_time"] = None
    vp["mfe_pct"] = 0.0
    vp["mae_pct"] = 0.0
    return vp


def update_virtual_strategy(
    vp: Optional[dict], strategy_name: str, now: datetime, action: Optional[str],
    price: Optional[float], target_pct: float, budget: float = 10_000_000.0,
) -> dict:
    """이 전략의 이번 사이클 액션(HYNIX/INVERSE/HOLD 계열 문자열)을 가상 포트폴리오에
    적용한다. 방향이 바뀌면 기존 포지션을 먼저 청산한 뒤 신규 진입한다."""
    vp = _reset_if_new_day(vp, now, budget)
    if price is None or price <= 0:
        return vp

    target_symbol = _target_symbol_from_action(action)

    if vp["symbol"] and vp["quantity"] > 0:
        pnl_pct_now = (price / vp["entry_price"] - 1.0) * 100.0 if vp["entry_price"] else 0.0
        vp["mfe_pct"] = max(vp.get("mfe_pct", pnl_pct_now), pnl_pct_now)
        vp["mae_pct"] = min(vp.get("mae_pct", pnl_pct_now), pnl_pct_now)

    if vp["symbol"] and target_symbol != vp["symbol"]:
        vp = _close_virtual_position(vp, price, now, strategy_name)

    if not vp["symbol"] and target_symbol and (target_pct or 0) > 0:
        invest_cash = vp["cash"] * (min(100.0, target_pct) / 100.0)
        qty = int(invest_cash // price)
        if qty >= 1:
            vp["symbol"] = target_symbol
            vp["quantity"] = qty
            vp["entry_price"] = price
            vp["entry_time"] = now.isoformat()
            vp["cash"] -= qty * price
            vp["mfe_pct"] = 0.0
            vp["mae_pct"] = 0.0

    return vp


def force_close_all(vp: dict, price: Optional[float], now: datetime, strategy_name: str) -> dict:
    """당일 15:15 강제청산과 동일하게 가상 포지션도 장 마감 전 전량 청산한다."""
    if vp.get("symbol") and price:
        vp = _close_virtual_position(dict(vp), price, now, strategy_name)
    return vp


def load_shadow_ledger(days: Optional[list] = None) -> pd.DataFrame:
    if not _SHADOW_LEDGER_PATH.exists():
        return pd.DataFrame(columns=_SHADOW_LEDGER_COLUMNS)
    try:
        df = pd.read_csv(_SHADOW_LEDGER_PATH, dtype={"symbol": str})
    except Exception as exc:
        logger.error("[StrategyShadowTracker] 로드 실패: %s", exc)
        return pd.DataFrame(columns=_SHADOW_LEDGER_COLUMNS)
    if df.empty:
        return df
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    df = df.dropna(subset=["exit_time"])
    if days:
        df = df[df["exit_time"].dt.strftime("%Y%m%d").isin(days)]
    return df.sort_values("exit_time").reset_index(drop=True)


def _stats_from_trades(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trade_count": 0, "win_rate": None, "total_return_pct": 0.0, "profit_factor": None,
            "max_drawdown_pct": None, "avg_holding_minutes": None, "avg_mfe_pct": None, "avg_mae_pct": None,
            "hynix_pnl_krw": 0.0, "inverse_pnl_krw": 0.0,
        }
    pnl_pct = pd.to_numeric(trades["pnl_pct"], errors="coerce").dropna()
    pnl_krw = pd.to_numeric(trades["pnl_krw"], errors="coerce").dropna()
    wins = pnl_krw[pnl_krw > 0]
    losses = pnl_krw[pnl_krw < 0]
    gross_profit, gross_loss = float(wins.sum()), float(abs(losses.sum()))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    win_rate = round(len(wins) / len(pnl_krw) * 100.0, 2) if len(pnl_krw) else None

    cum = pnl_krw.cumsum()
    running_peak = cum.cummax()
    drawdown = cum - running_peak
    max_dd = round(float(drawdown.min()), 2) if not drawdown.empty else 0.0

    holding = pd.to_numeric(trades["holding_minutes"], errors="coerce").dropna()
    mfe = pd.to_numeric(trades["mfe_pct"], errors="coerce").dropna()
    mae = pd.to_numeric(trades["mae_pct"], errors="coerce").dropna()

    hynix_pnl = float(pd.to_numeric(trades[trades["symbol"] == _SYMBOL_HYNIX]["pnl_krw"], errors="coerce").sum())
    inverse_pnl = float(pd.to_numeric(trades[trades["symbol"] == _SYMBOL_INVERSE]["pnl_krw"], errors="coerce").sum())

    return {
        "trade_count": int(len(trades)), "win_rate": win_rate,
        "total_return_pct": round(float(pnl_pct.sum()), 4), "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd, "avg_holding_minutes": round(float(holding.mean()), 1) if not holding.empty else None,
        "avg_mfe_pct": round(float(mfe.mean()), 4) if not mfe.empty else None,
        "avg_mae_pct": round(float(mae.mean()), 4) if not mae.empty else None,
        "hynix_pnl_krw": round(hynix_pnl, 2), "inverse_pnl_krw": round(inverse_pnl, 2),
    }


def compute_strategy_comparison_stats(days: Optional[list] = None, adaptive_fusion_real_stats: Optional[dict] = None) -> dict:
    """섹션 18 — 전략별(거래횟수/승률/수익률/PF/MDD/평균보유시간/MFE·MAE/종목별손익) 비교.

    ADAPTIVE_FUSION은 실제 공통 주문(execution ledger)이 있으면 그 값을 우선 사용하고,
    (adaptive_fusion_real_stats로 주입) 없으면 이 shadow 원장의 값으로 대체한다.
    """
    df = load_shadow_ledger(days)
    result = {}
    for strategy in ALL_STRATEGIES:
        trades = df[df["strategy_name"] == strategy] if not df.empty else df
        result[strategy] = _stats_from_trades(trades)
    if adaptive_fusion_real_stats:
        result[STRATEGY_ADAPTIVE_FUSION] = adaptive_fusion_real_stats
    return result


def compare_adaptive_fusion_vs_active_only(days: Optional[list] = None, adaptive_fusion_real_stats: Optional[dict] = None) -> dict:
    """섹션 19 — ADAPTIVE_FUSION이 ACTIVE_ONLY보다 Profit Factor 또는 순수익이
    개선됐는지 비교. 개선되지 않았으면 should_fallback=True를 반환한다(자동 fallback
    판단은 호출부가 이 결과로 수행한다)."""
    stats = compute_strategy_comparison_stats(days, adaptive_fusion_real_stats)
    active = stats[STRATEGY_ACTIVE_ONLY]
    fusion = stats[STRATEGY_ADAPTIVE_FUSION]

    def _pf_value(pf):
        if pf is None:
            return 0.0
        return pf if pf != float("inf") else 1e9

    fusion_pf, active_pf = _pf_value(fusion.get("profit_factor")), _pf_value(active.get("profit_factor"))
    fusion_net = fusion.get("total_return_pct", 0.0) or 0.0
    active_net = active.get("total_return_pct", 0.0) or 0.0

    improved = fusion_pf > active_pf or fusion_net > active_net
    return {
        "active_only": active, "adaptive_fusion": fusion, "improved": improved,
        "should_fallback": not improved,
        "reason": (
            f"ADAPTIVE_FUSION PF={fusion.get('profit_factor')} 순수익={fusion_net:.3f}% vs "
            f"ACTIVE_ONLY PF={active.get('profit_factor')} 순수익={active_net:.3f}% — "
            + ("개선됨" if improved else "개선되지 않음(자동 fallback 대상)")
        ),
    }
