"""T2 SHADOW — STRONG 후보의 T+2 가상 조기진입 기록 (2026-10-28, **기록 전용**).

주문·슬롯·state 판단에 **어떤 영향도 주지 않는다.** 실제 진입은 기존 T+3 경로 그대로다.
연구(research 2026-10-28 STRONG 조기판정): 9일 표본으로는 T+2 우위가 1건 의존이라
production ON 금지, 20~30영업일 또는 T+2 STRONG 20건 이상 누적 후 재검증한다.

정의 (기존 STRONG 그대로, 임계값 변경 없음)
  T+2 = 플래그봉 종료(D) + 2분 = MU Assist 진입 계측(mu_assist ENTRY_SHADOW)과 같은 시점.
  STRONG = MU ENTRY_CONFIRM(MU CONFIRM ∧ HY gap EXPAND) ∧ 체결강도 MATCH(그 시점 직전 60초 평균).
  T2_SHADOW = PASS(STRONG) / FAIL(MU 판정은 됐지만 STRONG 아님). MU 판정 불가면 기록하지 않는다.
  T2_ENTRY_PRICE = T+2 시점 대상 ETF 현재가 + 1호가(가상 매수 = ask 근사).
  T3 결과 = 같은 플래그의 T+3 판정(신호원장) — 체결이면 실제 체결가(E 돌파대기는 돌파 체결가).
  T2_vs_T3_DELTA = (T3 체결가 − T2 가상가) / T2 가상가 (bp). 양수 = T+3 가 더 비싸게 샀다(T+2 유리).
  T2_WHIPSAW = PASS 인데 T+3 확인 실패(config.TW_WHIPSAW_REJECT_REASONS)로 거절 = T+3 가 걸러준 휩쏘.
  그 외 게이트 거절(슬롯·시간창·오전 no-trade·TEG·VWAP …)은 t3_block_kind=GATE — T+2 였어도 같은 게이트에
  막혔으므로 휩쏘가 아니고 가상 손익 집계에서도 뺀다.
  가상 결과(PASS 만): T+2 가상가 기준 순수익(수수료 왕복 포함) +1.5% / −1.0% 중 먼저 닿은 것,
  30분 동안의 MFE/MAE. 가격은 매 tick 의 대상 ETF 현재가(= 매도가 근사).

원장: 신호원장과 같은 폴더의 ``macd2_t2_shadow.json`` (테스트는 conftest 가 tmp 로 돌린다).
이 모듈의 어떤 예외도 worker 가 감싸서 tick 을 깨지 않는다.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config, ledger
from app.trading.macd2.models import Direction

PASS = "PASS"
FAIL = "FAIL"
HIT_TP = "TP_+1.5"
HIT_SL = "SL_-1.0"
HIT_NONE = "NONE_30M"
BLOCK_CONFIRM = "CONFIRM"      # T+3 확인 실패 거절 = 휩쏘
BLOCK_GATE = "GATE"            # 다른 게이트 거절 = T+2 였어도 진입 불가 -> 가상 손익 집계 제외
TP_PCT = 1.5
SL_PCT = -1.0
TRACK_MIN = 30
FEE_RATE = 36.4e-6          # 원장과 같은 편도 수수료율
_LOCK = threading.Lock()
_FLUSH_SEC = 60


def is_enabled() -> bool:
    return bool(getattr(config, "T2_SHADOW_ENABLED", True))


def ledger_path() -> Path:
    return Path(ledger.SIGNAL_LEDGER_PATH).parent / config.T2_SHADOW_FILENAME


def base_id(signal_id: str) -> str:
    """T+3 / E 돌파 signal_id -> 플래그 id (YYYYMMDD_HHMMSS_DIR)."""
    return str(signal_id or "").split(":")[0]


def _etf_tick(price: float) -> float:
    return 1.0 if price < 2000 else 5.0          # KRX ETF 호가단위


def _load() -> dict[str, dict]:
    p = ledger_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _save(rows: dict[str, dict]) -> None:
    ledger._assert_safe_to_write_ledger()
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _net_pct(entry: float, price: float) -> float:
    return ((price - entry) - (entry + price) * FEE_RATE) / entry * 100.0


def record_t2(*, signal_id: str, direction: Direction, flag_bar_dt: datetime, now: datetime,
              mu_status: str, cttr: Optional[float], cttr_state: str, symbol: str,
              price: Optional[float]) -> Optional[dict]:
    """T+2 시점 1회. 이미 기록된 플래그면 아무 것도 하지 않는다. 기록한 행을 돌려준다."""
    from app.trading.macd2 import exec_strength, mu_assist
    bid = base_id(signal_id)
    with _LOCK:
        rows = _load()
        if bid in rows:
            return None
        strong = (mu_status == mu_assist.ENTRY_CONFIRM and cttr_state == exec_strength.MATCH)
        entry = (float(price) + _etf_tick(float(price))) if (strong and price) else None
        row = {
            "flag_id": bid, "t3_signal_id": signal_id, "trading_date": now.strftime("%Y%m%d"),
            "flag_bar_at": flag_bar_dt.isoformat(), "direction": getattr(direction, "value", str(direction)),
            "symbol": symbol, "T2_SHADOW": PASS if strong else FAIL,
            "mu_status": mu_status, "cttr": None if cttr is None else round(float(cttr), 2), "cttr_state": cttr_state,
            "t2_at": now.isoformat(), "T2_ENTRY_PRICE": entry,
            "t3_result": "", "t3_reason": "", "t3_block_kind": "", "t3_at": "", "T3_ENTRY_PRICE": None, "t3_qty": None,
            "T2_vs_T3_DELTA_BP": None, "T2_WHIPSAW": None,
            "first_hit": "", "first_hit_at": "", "mfe_pct": None, "mae_pct": None, "done": entry is None,
            "last_flush": now.isoformat(),
        }
        rows[bid] = row
        _save(rows)
        return dict(row)


def note_signal_result(signal_id: str, outcome, now: datetime, *, order_result: str = "",
                       block_reason: str = "", catchup: Optional[dict] = None) -> None:
    """신호원장 한 행이 기록될 때마다 — 이 플래그의 T+3(또는 E 돌파) 결과를 붙인다.
    ``order_result`` / ``block_reason`` 은 신호원장에 실제로 쓴 값(FILTERED_OUT 은 outcome 이 없다)."""
    from app.trading.macd2.models import SignalState
    sid = str(signal_id or "")
    if not (sid.endswith(":TW2_3SLOT_CONFIRM") or sid.endswith(":E_BRK")):
        return
    bid = base_id(sid)
    with _LOCK:
        rows = _load()
        row = rows.get(bid)
        if row is None:
            return
        executed = outcome is not None and outcome.final_state == SignalState.EXECUTED
        state = order_result or (outcome.final_state.value if outcome is not None else SignalState.WAITING.value)
        if sid.endswith(":E_BRK") and not executed:
            return                                   # 돌파 미체결은 T+3 판정(E 대기)을 덮지 않는다
        row["t3_result"] = ("E_BRK:" if sid.endswith(":E_BRK") else "") + str(state)
        row["t3_reason"] = block_reason or (str(getattr(outcome, "block_reason", "") or "") if outcome is not None else "")
        row["t3_at"] = now.isoformat()
        if catchup is not None:
            # MU CATCH-UP (2026-10-28): T+3 판정 시각 값. PROMOTE = XS STRONG ∨ CATCH-UP PASS.
            row["MU_CATCHUP"] = catchup.get("status")
            row["MU_SESSION_PCT"] = catchup.get("mu_session_pct")
            row["HY_SESSION_PCT"] = catchup.get("hy_session_pct")
            row["MU_HY_GAP"] = catchup.get("mu_hy_gap")
        row["PROMOTE"] = bool(row.get("T2_SHADOW") == PASS or row.get("MU_CATCHUP") == "PASS")
        if executed:
            px = float(getattr(outcome, "filled_avg_price", 0.0) or 0.0) or None
            row["T3_ENTRY_PRICE"] = px
            row["t3_qty"] = int(getattr(outcome, "quantity", 0) or 0)
            if px and row.get("T2_ENTRY_PRICE"):
                row["T2_vs_T3_DELTA_BP"] = round((px / float(row["T2_ENTRY_PRICE"]) - 1.0) * 1e4, 2)
        if row["T2_SHADOW"] == PASS:
            # 체결 = False / 대기(E 돌파대기 등 WAITING) = 아직 모름(None)
            # 거절: T+3 확인 실패(config.TW_WHIPSAW_REJECT_REASONS) = 휩쏘(True, T+2 라면 들어갔을 것)
            #       그 외 게이트(슬롯·시간창·오전 no-trade·TEG·VWAP …) = GATE(False, T+2 도 같은 게이트에 막힌다)
            if executed:
                row["T2_WHIPSAW"], row["t3_block_kind"] = False, ""
            elif state == SignalState.WAITING.value:
                row["T2_WHIPSAW"], row["t3_block_kind"] = None, ""
            elif row["t3_reason"] in config.TW_WHIPSAW_REJECT_REASONS:
                row["T2_WHIPSAW"], row["t3_block_kind"] = True, BLOCK_CONFIRM
            else:
                row["T2_WHIPSAW"], row["t3_block_kind"] = False, BLOCK_GATE
        _save(rows)


def update(now: datetime, quotes: dict) -> None:
    """PASS 행의 가상 결과 추적. 열린 행이 없으면 파일도 거의 건드리지 않는다(읽기 1회)."""
    with _LOCK:
        rows = _load()
        open_rows = [r for r in rows.values() if not r.get("done")]
        if not open_rows:
            return
        changed = False
        for r in open_rows:
            px = quotes.get(r["symbol"])
            t2 = datetime.fromisoformat(r["t2_at"])
            if px and float(px) > 0 and r.get("T2_ENTRY_PRICE"):
                n = _net_pct(float(r["T2_ENTRY_PRICE"]), float(px))
                if r["mfe_pct"] is None or n > r["mfe_pct"]:
                    r["mfe_pct"] = round(n, 3); changed = changed or not r["first_hit"]
                if r["mae_pct"] is None or n < r["mae_pct"]:
                    r["mae_pct"] = round(n, 3); changed = changed or not r["first_hit"]
                if not r["first_hit"]:
                    if n >= TP_PCT:
                        r["first_hit"], r["first_hit_at"], changed = HIT_TP, now.isoformat(), True
                    elif n <= SL_PCT:
                        r["first_hit"], r["first_hit_at"], changed = HIT_SL, now.isoformat(), True
            if now >= t2 + timedelta(minutes=TRACK_MIN):
                if not r["first_hit"]:
                    r["first_hit"] = HIT_NONE
                r["done"] = True; changed = True
            elif (now - datetime.fromisoformat(r["last_flush"])).total_seconds() >= _FLUSH_SEC:
                changed = True
            if changed:
                r["last_flush"] = now.isoformat()
        if changed:
            _save(rows)


def rows() -> list[dict]:
    with _LOCK:
        return sorted(_load().values(), key=lambda r: r.get("t2_at") or "")


def _virtual_ret(r: dict) -> Optional[float]:
    """가상 진입의 결과 수익률(%): +1.5 / −1.0 / 30분 미도달은 마지막 MFE·MAE 로 결정 불가 -> None."""
    if r.get("first_hit") == HIT_TP:
        return TP_PCT
    if r.get("first_hit") == HIT_SL:
        return SL_PCT
    return None


def summary(all_rows: Optional[list[dict]] = None) -> dict[str, Any]:
    """UI/연구 집계. 판정은 하지 않는다 — 재검증 조건 충족 여부만 알려준다."""
    rs = all_rows if all_rows is not None else rows()
    p_all = [r for r in rs if r.get("T2_SHADOW") == PASS]
    # GATE 거절(슬롯·시간창 등)은 T+2 였어도 들어가지 못했다 -> 가상 진입 집계에서 뺀다.
    p = [r for r in p_all if r.get("t3_block_kind") != BLOCK_GATE]
    deltas = [r["T2_vs_T3_DELTA_BP"] for r in p if r.get("T2_vs_T3_DELTA_BP") is not None]
    rets = [(r["trading_date"], _virtual_ret(r)) for r in p if _virtual_ret(r) is not None]
    gains = [x for _, x in rets if x > 0]; losses = [-x for _, x in rets if x < 0]
    by_day: dict[str, float] = {}
    for d, x in rets:
        by_day[d] = by_day.get(d, 0.0) + x
    eq = peak = mdd = 0.0
    for d in sorted(by_day):
        eq += by_day[d]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    whip = [r for r in p if r.get("T2_WHIPSAW")]
    days = sorted({r["trading_date"] for r in rs})
    tops = sorted((x for _, x in rets), reverse=True)
    return {
        "days": len(days), "flags": len(rs), "pass": len(p_all), "pass_gate_blocked": len(p_all) - len(p),
        "pass_effective": len(p),
        "whipsaw_added_pct": round(sum(TP_PCT for r in whip if r.get("first_hit") == HIT_TP), 2),
        "whipsaw_loss_pct": round(sum(SL_PCT for r in whip if r.get("first_hit") == HIT_SL), 2),
        "t3_filled": len(deltas),
        "t2_better": sum(1 for x in deltas if x > 0), "t2_worse": sum(1 for x in deltas if x < 0),
        "avg_delta_bp": round(sum(deltas) / len(deltas), 2) if deltas else None,
        "whipsaw": len(whip),
        "whipsaw_tp": sum(1 for r in whip if r.get("first_hit") == HIT_TP),
        "whipsaw_sl": sum(1 for r in whip if r.get("first_hit") == HIT_SL),
        "hit_tp": sum(1 for r in p if r.get("first_hit") == HIT_TP),
        "hit_sl": sum(1 for r in p if r.get("first_hit") == HIT_SL),
        "hit_none": sum(1 for r in p if r.get("first_hit") == HIT_NONE),
        "virtual_total_pct": round(sum(x for _, x in rets), 2),
        "virtual_pf": round(sum(gains) / sum(losses), 2) if losses else None,
        "virtual_winrate": round(len(gains) / len(rets), 3) if rets else None,
        "virtual_mdd_pct": round(mdd, 2),
        "by_day_pct": {d: round(v, 2) for d, v in sorted(by_day.items())},
        "catchup_pass": sum(1 for r in rs if r.get("MU_CATCHUP") == "PASS"),
        "promote": sum(1 for r in rs if r.get("PROMOTE")),
        "total_ex_top2_pct": round(sum(tops[2:]), 2) if len(tops) > 2 else None,
        "ready_for_review": len(days) >= int(config.T2_SHADOW_REVIEW_MIN_DAYS) or len(p_all) >= int(config.T2_SHADOW_REVIEW_MIN_PASS),
    }
