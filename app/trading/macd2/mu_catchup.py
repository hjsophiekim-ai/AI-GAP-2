"""MU CATCH-UP — MU 선행 · 하이닉스 지연추종 RED 의 좁은 실거래 진입 예외 (2026-10-28).

XS(STRONG) 가 "지금 MU 와 하이닉스가 같이 강하다" 를 살린다면, CATCH-UP 은
"MU 는 오전부터 확실히 올랐는데 하이닉스가 아직 1%p 이상 못 따라간 RED" 만 살린다.
연구: 10/19~10/28 에서 해당 거절 플래그는 10/28 09:39 RED 1건뿐 — 임계값은 그 연구에서
고정한 값 그대로이며 최적화하지 않는다(config.MU_CATCHUP_*).

CATCHUP PASS (전부)
  · MU Assist 가 켜져 있음 (MU Assist 에 내장, 별도 토글 없음 — XS 토글과 무관) ∧ MACD2_MU_CATCHUP_KILL 아님
  · RED(UP_RED) 플래그, 플래그봉 시작이 09:00~11:00
  · MU 세션 방향수익률 = MU(t)/MU(08:00 기준봉) − 1 ≥ +0.5%
  · MU − 하이닉스 방향괴리 ≥ +1.0%p (하이닉스도 같은 08:00 기준봉)
  · 체결강도 RED 방향일치 (exec_strength, 판정 시각 직전 60초 평균)
  · MU/하이닉스/체결강도 모두 fresh — 하나라도 없거나 stale 이면 UNAVAILABLE(= 기존 CHOP 그대로)
  t = 판정 시각, 값은 t 에 **끝난** 1분봉 종가만 쓴다(미래 데이터 금지).

PASS 일 때 worker 가 풀어주는 것은 셋뿐이다:
  ① T+3 확인 2종 (REJECT_NOT_CONFIRMED / REJECT_MACD_GAP_NOT_EXPANDING) — 나머지 시간창·간격·W2 reset·
    품질 판정은 evaluate_time_window_entry(skip_confirmation_check=True) 로 그대로 한다.
  ② CHOP_MORNING_NO_TRADE (MC-DELAY)
  ③ MU Assist 단기 ENTRY_BAD
EMA/VWAP veto · 슬롯/하루 한도 · 시간창 · 보유 중복 · 주문가능금액/예탁금 · 안전/주문 gate · 장마감 차단은 손대지 않는다.
BLUE 는 값만 계산해 기록한다(FAIL: BLUE_SHADOW_ONLY) — 실거래 적용 없음.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import config, ledger
from app.trading.macd2.models import Direction

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"
GATE_CONFIRMATION = "T3_CONFIRMATION"
GATE_MORNING_NO_TRADE = "CHOP_MORNING_NO_TRADE"
GATE_MU_ENTRY_BAD = "MU_ASSIST_ENTRY_BAD"
LAST_FILENAME = "macd2_mu_catchup_last.json"
_LOCK = threading.Lock()


def is_active(state: Any) -> bool:
    """CATCH-UP 판정을 해도 되는가 — MU Assist 가 켜져 있고 kill switch 가 꺼져 있을 때만(MU Assist 내장)."""
    if bool(getattr(config, "MU_CATCHUP_KILL", False)):
        return False
    from app.trading.macd2 import mu_assist
    return mu_assist.is_enabled(state)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(config.KST).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _session_return(series: pd.Series, day: datetime, now_naive: datetime, max_age_min: int):
    """(기준값, 현재값, 사유). series index = 분 시작(naive KST), 값 = 종가. now 에 끝난 봉만."""
    if series is None or series.empty:
        return None, None, "MISSING"
    done = series[series.index + pd.Timedelta(minutes=1) <= now_naive]
    base_at = day.replace(hour=config.MU_CATCHUP_SESSION_BASE.hour, minute=config.MU_CATCHUP_SESSION_BASE.minute)
    after = done[done.index >= base_at]
    if after.empty or after.index[0] > base_at + timedelta(minutes=int(config.MU_CATCHUP_BASE_MAX_DELAY_MIN)):
        return None, None, "SESSION_INCOMPLETE"
    if done.index[-1] + timedelta(minutes=1) < now_naive - timedelta(minutes=int(max_age_min)):
        return None, None, "STALE"
    return float(after.iloc[0]), float(done.iloc[-1]), ""


def _hy_series(df_1m) -> Optional[pd.Series]:
    if df_1m is None or len(df_1m) == 0 or "datetime" not in df_1m or "close" not in df_1m:
        return None
    ts = pd.to_datetime(df_1m["datetime"], errors="coerce")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(config.KST).dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df_1m["close"], errors="coerce").values, index=ts).dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def evaluate(direction: Direction, flag_bar_dt: datetime, now: datetime, df_1m,
             mu_series: Optional[pd.Series] = None, cttr: Optional[dict] = None) -> dict[str, Any]:
    """판정 dict: status(PASS/FAIL/UNAVAILABLE), reason, mu_session_pct, hy_session_pct, mu_hy_gap, cttr, cttr_match.
    kill/토글은 호출자가 is_active 로 먼저 본다. 어떤 값이든 계산할 수 있으면 기록용으로 채운다."""
    from app.trading.macd2 import exec_strength, mu_assist
    d = Direction(getattr(direction, "value", direction))
    sign = 1.0 if d == Direction.UP_RED else -1.0
    now_n = _naive(now)
    day = now_n.replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, Any] = {"status": UNAVAILABLE, "reason": "", "direction": d.value,
                           "flag_bar_at": flag_bar_dt.isoformat(), "evaluated_at": now.isoformat(),
                           "mu_session_pct": None, "hy_session_pct": None, "mu_hy_gap": None,
                           "cttr": None, "cttr_match": None}
    mu = mu_series if mu_series is not None else mu_assist.load_mu_1m(now)
    m0, m1, mr = _session_return(mu, day, now_n, config.MU_CATCHUP_MU_MAX_AGE_MIN)
    h0, h1, hr = _session_return(_hy_series(df_1m), day, now_n, config.MU_CATCHUP_HY_MAX_AGE_MIN)
    cs = cttr if cttr is not None else exec_strength.evaluate(d, now)
    out["cttr"] = None if cs.get("cttr") is None else round(float(cs["cttr"]), 2)
    if cs.get("state") != exec_strength.UNAVAILABLE:
        out["cttr_match"] = cs.get("state") == exec_strength.MATCH
    if m0 is not None:
        out["mu_session_pct"] = round(sign * (m1 / m0 - 1.0) * 100.0, 3)
    if h0 is not None:
        out["hy_session_pct"] = round(sign * (h1 / h0 - 1.0) * 100.0, 3)
    if out["mu_session_pct"] is not None and out["hy_session_pct"] is not None:
        out["mu_hy_gap"] = round(out["mu_session_pct"] - out["hy_session_pct"], 3)
    if mr:
        out["reason"] = f"MU_{mr}"; return out
    if hr:
        out["reason"] = f"HY_{hr}"; return out
    if out["cttr_match"] is None:
        out["reason"] = "CTTR_UNAVAILABLE"; return out
    fails = []
    if d != Direction.UP_RED:
        fails.append("BLUE_SHADOW_ONLY")
    ft = _naive(flag_bar_dt).time()
    if not (config.MU_CATCHUP_WINDOW_START <= ft < config.MU_CATCHUP_WINDOW_END):
        fails.append("OUTSIDE_0900_1100")
    if out["mu_session_pct"] < float(config.MU_CATCHUP_MU_MIN_PCT):
        fails.append("MU_SESSION_LT_0.5")
    if out["mu_hy_gap"] < float(config.MU_CATCHUP_GAP_MIN_PCT):
        fails.append("GAP_LT_1.0")
    if not out["cttr_match"]:
        fails.append("CTTR_MISMATCH")
    out["status"] = FAIL if fails else PASS
    out["reason"] = ",".join(fails)
    return out


def ledger_fields(ev: Optional[dict], overridden: list[str]) -> dict[str, Any]:
    """신호원장 컬럼 — 왜 진입(또는 미진입)했는지."""
    if ev is None:
        return {}
    return {
        "mu_catchup_pass": ev["status"],
        "mu_catchup_reason": ev.get("reason") or "",
        "mu_session_pct": "" if ev["mu_session_pct"] is None else ev["mu_session_pct"],
        "hy_session_pct": "" if ev["hy_session_pct"] is None else ev["hy_session_pct"],
        "mu_hy_gap": "" if ev["mu_hy_gap"] is None else ev["mu_hy_gap"],
        "cttr_match": "" if ev["cttr_match"] is None else ev["cttr_match"],
        "catchup_overridden_gates": "|".join(overridden),
    }


def _last_path() -> Path:
    return Path(ledger.SIGNAL_LEDGER_PATH).parent / LAST_FILENAME


def remember_last(signal_id: str, ev: dict, overridden: list[str]) -> None:
    """UI 표시용 마지막 판정 1건."""
    with _LOCK:
        ledger._assert_safe_to_write_ledger()
        p = _last_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"signal_id": signal_id, **ev, "overridden": overridden}
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def last() -> Optional[dict]:
    p = _last_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
