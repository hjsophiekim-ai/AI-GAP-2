"""MACD2 보유 포지션 패널 — **읽기 전용** 표시 데이터 조립 (2026-09-29).

배경: 2026-09-29 P3 모드에서 CHOP 으로 진입한 두 포지션이 재시도 체결 버그로
BASE 관리가 됐는데, 화면만 보고는 "어떤 장세로 들어가 지금 어떤 청산 규칙이
걸려 있는가"를 알 수 없었다. 이 모듈은 그것을 한눈에 보이게 하는 값만 만든다.

원칙
  * 주문/진입/청산/전략 판단을 하지 않는다. state 를 바꾸지 않는다.
  * 모든 값은 **실제 state 필드 또는 worker 가 쓰는 바로 그 판정 함수**의 결과다
    (p3_stack / early_take_profit / small_whipsaw_hold / peak_protection /
    n1_adaptive). 새 판정식을 만들지 않는다.
  * 모르는 것은 추정하지 않고 ``UNKNOWN`` 으로 둔다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.trading.macd2 import (
    config, early_take_profit, n1_adaptive, p3_stack, peak_protection, small_whipsaw_hold,
)
from app.trading.trading_cost_engine import TradeCostEngine

UNKNOWN = "UNKNOWN"

SYMBOL_LABELS = {
    config.LONG_SYMBOL: "레버리지",
    config.INVERSE_SYMBOL: "인버스",
}
SYMBOL_DIRECTIONS = {
    config.LONG_SYMBOL: "UP_RED",
    config.INVERSE_SYMBOL: "DOWN_BLUE",
}


def net_return_pct(symbol: str, entry_price: float, current_price: float, quantity: int) -> Optional[float]:
    """worker._net_return_pct 와 **같은 식**(TradeCostEngine 시장가 왕복 비용)."""
    if not entry_price or not quantity or not current_price or entry_price <= 0 or current_price <= 0:
        return None
    cost = TradeCostEngine().compute_net_pnl(
        symbol, entry_price, current_price, quantity, buy_order_type="market", sell_order_type="market",
    )
    return float(cost["net_pnl"]) / (entry_price * quantity) * 100.0


def _parse(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _minutes_since(start: Optional[datetime], now: datetime) -> Optional[float]:
    if start is None:
        return None
    try:
        return max(0.0, (now - start).total_seconds() / 60.0)
    except TypeError:
        return None


def _entry_bar_chop(state) -> str:
    """진입봉 CHOP 판정(``time_window_entry_chop``) 표시값.

    True 는 확실하다. False 는 "판정 결과 NO" 와 "판정이 아예 기록되지 않음"
    (브로커 입양/재시도 버그 경로는 이 값을 계산 없이 False 로 둔다)을 구분할 수
    없으므로, 정상 3-SLOT 진입 후처리가 돈 흔적(P3 진입 스냅샷)이 있을 때만 NO,
    아니면 UNKNOWN 으로 보인다.
    """
    if bool(getattr(state, "time_window_entry_chop", False)):
        return "YES"
    if getattr(state, "p3_entry_regime", None):
        return "NO"
    return UNKNOWN


def build_position_panel(state, *, current_price: Optional[float], now: datetime) -> Optional[dict[str, Any]]:
    """보유 포지션이 없으면 None. 있으면 화면에 그릴 값 전부."""
    pos = getattr(state, "position", None)
    if pos is None or int(getattr(pos, "quantity", 0) or 0) <= 0:
        return None
    symbol = str(pos.symbol)
    entry_at = _parse(getattr(pos, "entry_at", None)) or _parse(getattr(state, "last_time_window_entry_at", None))
    elapsed = _minutes_since(entry_at, now)
    net = net_return_pct(symbol, float(pos.avg_price or 0.0), float(current_price or 0.0), int(pos.quantity))

    # ── ENTRY CONTEXT ────────────────────────────────────────────────────
    entry_regime = getattr(state, "p3_entry_regime", None) or UNKNOWN
    entry_bar_chop = _entry_bar_chop(state)
    entry = {
        # P3 진입 스냅샷은 진입 시 P3 가 켜져 있었을 때만 찍힌다 -- 없으면 모른다.
        "selected_mode": "P3" if getattr(state, "p3_entry_regime", None) else UNKNOWN,
        "entry_regime": entry_regime,
        "entry_bar_chop": entry_bar_chop,
    }

    # ── POSITION MODE / 관리 계층 ─────────────────────────────────────────
    mode = p3_stack.position_mode(state)
    governs = bool(p3_stack.governs_position(state))
    is_h30 = bool(p3_stack.is_h30(state))

    # ── EARLY TP (조기익절) ──────────────────────────────────────────────
    etp_active = bool(early_take_profit.is_active(state))
    trigger, floor = early_take_profit.thresholds(state)
    peak = float(getattr(state, "early_tp_peak_net_return", 0.0) or 0.0)
    eligible = etp_active and entry_bar_chop == "YES"
    armed = eligible and peak >= float(trigger)
    if eligible:
        etp_status = "ARMED" if armed else "ELIGIBLE"
        etp_reason = ""
    elif not etp_active:
        etp_status, etp_reason = "OFF", "조기익절 비활성 (3-SLOT 포지션관리 아님)"
    else:
        etp_status, etp_reason = "OFF", f"Entry Bar CHOP = {entry_bar_chop}"
    early_tp = {
        "status": etp_status, "eligible": eligible, "armed": armed, "reason": etp_reason,
        "peak_pct": peak if etp_active else None,
        "arm_pct": float(trigger), "floor_pct": float(floor),
    }

    # ── H50 ──────────────────────────────────────────────────────────────
    h50_mode_on = bool(small_whipsaw_hold.is_active(state))
    h50_holding = h50_mode_on and bool(small_whipsaw_hold.is_holding(state))
    h50_started = _parse(getattr(state, "h50_hold_started_at", None))
    h50 = {
        "status": "HOLD" if h50_holding else ("STANDBY" if h50_mode_on else "OFF"),
        "started_at": h50_started,
        "elapsed_min": _minutes_since(h50_started, now) if h50_holding else None,
        "max_hold_min": float(config.H50_MAX_HOLD_MIN),
        "trend_break_count": int(getattr(state, "h50_trend_break_count", 0) or 0),
        "trend_break_needed": int(config.H50_TREND_BREAK_BARS),
        "original_direction": getattr(state, "h50_original_direction", None),
    }

    # ── C1 / N1 ─────────────────────────────────────────────────────────
    c1_on = bool(peak_protection.is_active(state))
    c1_status = ("ARMED" if peak_protection.is_armed(state) else "ACTIVE") if c1_on else "OFF"
    n1_on = bool(n1_adaptive.is_active(state))
    if not n1_on:
        n1_status = "OFF"
    elif governs:
        n1_status = "OFF"          # B3 관리 중에는 N1 틱 익절 래더를 B3 가 대체한다
    else:
        n1_status = "ACTIVE"

    # ── B3 / Q2 / H30 / Y3 ───────────────────────────────────────────────
    rescue_max = float(config.P3_RESCUE_MAX_MIN)
    if bool(getattr(state, "p3_tp_rescued", False)):
        q2_status = "USED"
    elif governs and (is_h30 or (elapsed is not None and elapsed <= rescue_max)):
        q2_status = "ACTIVE"
    else:
        q2_status = "OFF"
    if bool(getattr(state, "y3_promoted", False)):
        y3_status = "PROMOTED"
    elif governs:
        y3_status = "PENDING"
    else:
        y3_status = "OFF"

    management = [
        ("B3", "ACTIVE" if governs else "OFF"),
        ("Q2", q2_status),
        ("H30", "ACTIVE" if is_h30 else "OFF"),
        ("Y3", y3_status),
        ("N1", n1_status),
        ("C1", c1_status),
        ("H50", h50["status"]),
        ("EARLY TP", etp_status),
    ]

    b3 = None
    if governs:
        b3 = {
            "elapsed_min": elapsed,
            "tp_pct": float(config.P3_B3_TP_PCT),
            "sl_pct": float(config.P3_B3_SL_PCT),
            "max_hold_min": float(config.P3_B3_MAX_HOLD_MIN),
            "rescue_max_min": rescue_max,
        }
    h30 = None
    if is_h30:
        h30 = {
            "deadline_at": _parse(getattr(state, "p3_h30_deadline_at", None)),
            "ext_max_hold_min": float(config.P3_H30_EXT_MAX_HOLD_MIN),
        }
    runner = None
    if mode == p3_stack.MODE_Y3_RUNNER:
        runner = {"kind": "Y3", "promoted_at": _parse(getattr(state, "y3_promoted_at", None))}
    elif mode == p3_stack.MODE_P3_RUNNER:
        runner = {"kind": "Q2", "promoted_at": _parse(getattr(state, "p3_tp_rescued_at", None))}

    return {
        "position": {
            "symbol": symbol,
            "label": SYMBOL_LABELS.get(symbol, symbol),
            "direction": SYMBOL_DIRECTIONS.get(symbol, UNKNOWN),
            "entry_at": entry_at,
            "entry_price": float(pos.avg_price or 0.0),
            "quantity": int(pos.quantity),
            "current_price": float(current_price) if current_price else None,
            "net_pct": net,
            "elapsed_min": elapsed,
        },
        "entry": entry,
        "position_mode": mode,
        "management": management,
        "early_tp": early_tp,
        "h50": h50,
        "b3": b3,
        "h30": h30,
        "runner": runner,
    }


# ── 렌더 ──────────────────────────────────────────────────────────────────
def _hm(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M:%S") if isinstance(dt, datetime) else "-"


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def render_lines(panel: dict[str, Any]) -> list[str]:
    """패널을 markdown 줄 목록으로. Streamlit 과 무관한 순수 함수(테스트용)."""
    p, e, etp, h50 = panel["position"], panel["entry"], panel["early_tp"], panel["h50"]
    cur = "-" if p["current_price"] is None else f"{p['current_price']:,.0f}"
    lines = [
        f"**POSITION** · {p['symbol']} {p['label']} · {p['direction']} · "
        f"{p['quantity']:,}주 @ {p['entry_price']:,.0f} · 진입 {_hm(p['entry_at'])} · "
        f"현재가 {cur} · "
        f"순수익 **{_pct(p['net_pct'])}**",
        f"**POSITION MODE: {panel['position_mode']}**",
        f"**ENTRY CONTEXT** · Mode {e['selected_mode']} · Regime {e['entry_regime']} · "
        f"Entry Bar CHOP {e['entry_bar_chop']} · Early TP Eligible {'YES' if etp['eligible'] else 'NO'}",
        "**ACTIVE MANAGEMENT** · " + " · ".join(f"{k} `{v}`" for k, v in panel["management"]),
    ]
    etp_line = f"**EARLY TP: {etp['status']}**"
    if etp["status"] == "OFF":
        etp_line += f" · Reason: {etp['reason']}"
    else:
        etp_line += (f" · Peak {_pct(etp['peak_pct'])} · Arm +{etp['arm_pct']:.2f}% · "
                     f"Exit floor +{etp['floor_pct']:.2f}% (완성 3분봉 종가)")
    lines.append(etp_line)
    if h50["status"] == "HOLD":
        el = h50["elapsed_min"]
        el_txt = "-" if el is None else f"{el:.0f}"
        lines.append(
            f"**H50 HOLD: ACTIVE** · Started {_hm(h50['started_at'])} · "
            f"Elapsed {el_txt}m / {h50['max_hold_min']:.0f}m · "
            f"Trend break {h50['trend_break_count']}/{h50['trend_break_needed']} · "
            f"Original {h50['original_direction'] or '-'}")
    else:
        lines.append(f"**H50: {h50['status']}**")
    if panel["b3"]:
        b = panel["b3"]
        el = b["elapsed_min"]
        el_txt = "-" if el is None else f"{el:.1f}"
        lines.append(
            f"**B3** · 진입 후 {el_txt}분 · TP +{b['tp_pct']:.1f}% / "
            f"SL -{b['sl_pct']:.1f}% · max-hold {b['max_hold_min']:.0f}분 (Q2 rescue {b['rescue_max_min']:.0f}분 이내 +{b['tp_pct']:.1f}%)")
    if panel["h30"]:
        h = panel["h30"]
        lines.append(f"**H30: ACTIVE** · deadline {_hm(h['deadline_at'])} · "
                     f"{h['ext_max_hold_min']:.0f}분 시점 Y3 재평가 예정")
    if panel["runner"]:
        r = panel["runner"]
        lines.append(f"**{'Y3-RUNNER' if r['kind'] == 'Y3' else 'P3-RUNNER'}** · promoted {_hm(r['promoted_at'])} · 이후 N1/C1 관리")
    return lines
