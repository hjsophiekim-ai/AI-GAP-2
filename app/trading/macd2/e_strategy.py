"""E 전략 — EARLY-UP-FAST + RS125 (2026-10-05).

E 는 **P3 를 기반으로 하는 독립 전략**이다. P3 의 signal / T+3 / gate / slot /
regime / B3·Q2·Y3·H30·H50 / 청산 로직을 한 줄도 바꾸지 않고, 그 위에 두 겹만
얹는다:

    ① EARLY-PASS      T+3 승인 즉시 사는 대신, '이미 돌파 임박 + 되돌림 없음 +
                      MACD gap 확대' 세 조건을 전부 만족할 때만 즉시 진입한다.
                      미달이면 (플래그봉, 확정봉) 극값을 trigger 로 걸어 최대
                      15분 대기하고, 돌파하면 그 시점에 진입, 미도달이면 폐기한다.
    ② RS125           진입 시점 RS 점수가 과거 진입분포의 상위 20% 면 주문
                      budget 에 x1.25 를 곱한다. 일일 누적한도 안에서만 올리고,
                      증액분은 누계에 즉시 반영한다.

EARLY-UP-FAST 는 ①의 거리기준을 **UP 에서만** 0.2% -> 0.3% 로 넓힌 것이다.
DOWN 은 한 글자도 바뀌지 않고, c2/c3 **폐기조건은 양방향 모두 불변**이다 --
c2=False 로 폐기되던 신호(10/02 09:54 등)는 E 에서도 반드시 폐기된다.

이 모듈이 절대 하지 않는 것
---------------------------
진입 **승인** 판단을 하지 않는다. MACD zero-cross 검출, T+3 재확인, TW2 veto,
슬롯 배정, Trend Quality, TEG, CHOP 판정, 청산(TP/SL/trailing/ETP/반대신호)은
이 파일에서 import 조차 하지 않는 영역이다. 여기 있는 함수는 **이미 승인된
진입을 지금 낼지 / 조건부로 미룰지 / 얼마로 낼지**만 답한다.

E 가 꺼져 있으면(=N1/P3 모드) 모든 공개 함수가 즉시 중립값을 돌려준다 --
:func:`is_active` 가 False 면 worker 의 호출부 자체가 no-op 이 된다.

순수 함수 + state 기록 함수만 있다(``position_sizing.py`` 와 같은 계약):
네트워크/브로커 접근 없음, 입력 프레임 변경 없음, 주어진 데이터 이후를
내다보지 않음. ``note_*`` / ``arm_*`` / ``clear_*`` 만 state 를 갱신한다.

RS 통계의 출처
--------------
RS 는 **과거 영업일의 진입들**로 만든 z통계/임계를 쓴다(expanding window,
과거표본 30건 이상, 상위 20%). 미래 데이터는 쓰지 않는다. 운영에서는
:func:`note_rs_sample` 이 승인 시점마다 원자료를 state 에 쌓고
:func:`rs_stats_for_day` 가 '그 날 이전' 표본만으로 통계를 만든다.

``config.E_RS_TABLE_PATH`` 가 지정되면 그 파일의 사전계산 표를 대신 쓴다 --
연구 하네스와의 **거래단위 parity 검증** 전용 경로다. 운영 기본값은 None 이고,
그때는 위의 라이브 집계만 쓴다.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import strategy_mode
from app.trading.macd2.models import Direction

logger = logging.getLogger(__name__)

#: RS 원자료 컬럼과 부호. ``-min_open`` 은 '이른 진입일수록 높은 점수'.
RS_COLUMNS = ("atr60", "min_open", "xc30")
RS_SIGNS = {"atr60": 1.0, "min_open": -1.0, "xc30": 1.0}

#: pending 폐기 사유 라벨 — 진단/원장에 그대로 남는다.
PENDING_EXPIRED = "E_PENDING_EXPIRED"
PENDING_EXPIRED_EOD = "E_PENDING_EXPIRED_EOD"
PENDING_EXPIRED_DATE_ROLLOVER = "E_PENDING_EXPIRED_DATE_ROLLOVER"
PENDING_FIRED = "E_PENDING_FIRED"
PENDING_SUPERSEDED = "E_PENDING_SUPERSEDED"

#: 대기 체결로 들어간 진입의 signal_id 접미사. 승인 시점의 원 signal_id 와
#: 구분해야 processed_signal_ids 중복판정에 걸리지 않는다.
BREAKOUT_SUFFIX = ":E_BRK"


@dataclass(frozen=True)
class EarlyPassDecision:
    """T+3 승인 직후의 판정. ``immediate`` 가 True 면 지금 산다."""
    evaluated: bool            # 판정을 실제로 수행했는가(데이터 부족이면 False)
    immediate: bool            # 즉시진입
    trigger: Optional[float]   # 대기로 갈 때 걸 trigger 가격
    dist_pct: Optional[float]  # trigger 까지 거리(%) — 음수면 이미 넘었다
    c1: bool                   # 거리조건
    c2: bool                   # 확정봉 동방향/중립
    c3: bool                   # MACD gap 확대
    up: bool
    upfast: bool               # UP 완화구간(0.2% < dist <= 0.3%)으로 통과했는가
    reason: str


NEUTRAL_EARLY_PASS = EarlyPassDecision(
    evaluated=False, immediate=False, trigger=None, dist_pct=None,
    c1=False, c2=False, c3=False, up=False, upfast=False, reason="NOT_ACTIVE",
)


@dataclass(frozen=True)
class RsDecision:
    """RS 증액 판정. ``multiplier`` 가 실제 budget 배수에 곱해진다."""
    active: bool
    hit: bool
    score: Optional[float]
    threshold: Optional[float]
    samples: int
    multiplier: float          # 1.0 또는 config.E_RS_MULT
    reason: str


NEUTRAL_RS = RsDecision(active=False, hit=False, score=None, threshold=None,
                        samples=0, multiplier=1.0, reason="NOT_ACTIVE")


# ── 활성 판정 ────────────────────────────────────────────────────────────

def is_active(state) -> bool:
    """E 모드인가. N1/P3 에서는 항상 False 이므로 호출부가 통째로 no-op 이 된다."""
    if not bool(getattr(config, "E_STRATEGY_ENABLED", True)):
        return False
    return strategy_mode.current(state) == strategy_mode.MODE_E


# ── ① EARLY-PASS ────────────────────────────────────────────────────────

def macd_gap_series(bars_3m) -> np.ndarray:
    """완성 3분봉 종가의 MACD 히스토그램(gap) 시계열."""
    c = pd.Series(bars_3m["close"].astype(float).to_numpy())
    fast = int(config.E_MACD_FAST)
    slow = int(config.E_MACD_SLOW)
    sig = int(config.E_MACD_SIGNAL)
    m = c.ewm(span=fast, adjust=False).mean() - c.ewm(span=slow, adjust=False).mean()
    return (m - m.ewm(span=sig, adjust=False).mean()).to_numpy()


def trigger_price(bars_3m, flag_bar_dt, confirm_bar_dt, direction) -> Optional[float]:
    """(플래그봉, 확정봉) 극값 ± 버퍼 = 돌파 trigger.

    UP 은 두 봉 고가의 최댓값 + 버퍼, DOWN 은 두 봉 저가의 최솟값 − 버퍼.
    두 봉을 모두 못 찾으면 None(=대기를 걸 수 없음)."""
    if bars_3m is None or not len(bars_3m):
        return None
    ts = pd.to_datetime(bars_3m["datetime"])
    rows = []
    for dt in (flag_bar_dt, confirm_bar_dt):
        if dt is None:
            continue
        mm = bars_3m[ts == pd.Timestamp(dt)]
        if len(mm):
            rows.append(mm.iloc[-1])
    if not rows:
        return None
    buf = float(config.E_TRIGGER_BUFFER_TICKS) * float(config.E_WATCH_TICK_SIZE)
    if direction == Direction.UP_RED:
        return max(float(r["high"]) for r in rows) + buf
    return min(float(r["low"]) for r in rows) - buf


def evaluate_early_pass(*, bars_3m, flag_bar_dt, confirm_bar_dt,
                        direction, price: Optional[float]) -> EarlyPassDecision:
    """T+3 승인 직후 '지금 살지 / 돌파를 기다릴지'.

    세 조건을 **전부** 만족해야 즉시진입이다:

      c1  현재가가 trigger 의 임계거리 이내 (이미 넘었으면 거리가 음수 -> 통과)
          UP 은 ``E_EARLY_PASS_DIST_UP_PCT``(0.3%), DOWN 은
          ``E_EARLY_PASS_DIST_DOWN_PCT``(0.2%). **UP 만 완화된 값**이다.
      c2  확정봉 몸통이 플래그 방향과 같거나 중립
      c3  MACD gap 이 플래그봉 이후 확대

    c2/c3 는 UP/DOWN 양쪽 모두 **그대로 요구**한다 -- 폐기조건을 완화하지
    않는다는 것이 이 규칙의 핵심 안전장치다.
    """
    if bars_3m is None or not len(bars_3m):
        return EarlyPassDecision(
            evaluated=False, immediate=False, trigger=None, dist_pct=None,
            c1=False, c2=False, c3=False, up=(direction == Direction.UP_RED),
            upfast=False, reason="NO_BARS")
    trig = trigger_price(bars_3m, flag_bar_dt, confirm_bar_dt, direction)
    if trig is None or price is None or price <= 0:
        return EarlyPassDecision(
            evaluated=False, immediate=False, trigger=trig, dist_pct=None,
            c1=False, c2=False, c3=False, up=(direction == Direction.UP_RED),
            upfast=False, reason="NO_TRIGGER_OR_PRICE")
    up = (direction == Direction.UP_RED)
    dist = ((trig - price) if up else (price - trig)) / price * 100.0
    limit = float(config.E_EARLY_PASS_DIST_UP_PCT if up
                  else config.E_EARLY_PASS_DIST_DOWN_PCT)
    c1 = bool(dist <= limit)

    ts = pd.to_datetime(bars_3m["datetime"])
    cb = bars_3m[ts == pd.Timestamp(confirm_bar_dt)] if confirm_bar_dt is not None else None
    if cb is not None and len(cb):
        r = cb.iloc[-1]
        body = (float(r["close"]) - float(r["open"])) / float(r["open"]) * 100.0
        c2 = bool(body >= 0) if up else bool(body <= 0)
    else:
        c2 = False

    g = macd_gap_series(bars_3m)
    fb = bars_3m[ts == pd.Timestamp(flag_bar_dt)] if flag_bar_dt is not None else None
    if fb is not None and len(fb) and len(g) >= 2:
        fi = int(fb.index[-1]) if fb.index[-1] < len(g) else len(g) - 2
        c3 = bool(abs(float(g[-1])) > abs(float(g[fi])))
    else:
        c3 = False

    immediate = bool(c1 and c2 and c3)
    narrow = float(config.E_EARLY_PASS_DIST_DOWN_PCT)
    upfast = bool(up and c2 and c3 and narrow < dist <= limit)
    return EarlyPassDecision(
        evaluated=True, immediate=immediate, trigger=float(trig),
        dist_pct=round(float(dist), 4), c1=c1, c2=c2, c3=c3, up=up, upfast=upfast,
        reason=("IMMEDIATE" if immediate else
                f"WAIT:c1={int(c1)},c2={int(c2)},c3={int(c3)}"),
    )


# ── pending trigger 상태 (재시작 복원 대상) ──────────────────────────────

def pending_record(state) -> Optional[dict]:
    """지금 걸려 있는 대기주문. 없으면 None."""
    rec = getattr(state, "e_pending", None)
    return dict(rec) if isinstance(rec, dict) and rec else None


def arm_pending(state, *, direction: Direction, trigger: float, signal_id: str,
                flag_bar_dt: datetime, confirm_bar_dt: datetime, now: datetime,
                session: Optional[str] = None,
                slot_number: Optional[int] = None,
                gate: Optional[dict] = None) -> dict:
    """돌파 대기를 건다 -- **슬롯/예산은 아직 소비하지 않는다.**

    state 에 그대로 직렬화되므로 worker 재시작 후에도 복원된다. 같은 시점에
    두 개를 걸 수 없다(새 것이 기존 것을 대체하고 기존 것은 SUPERSEDED 로
    기록된다) -- 한 번에 한 포지션만 보유하는 기존 계약과 같다.
    """
    prev = pending_record(state)
    if prev is not None:
        logger.info("[MACD2][E] pending superseded %s -> %s",
                    prev.get("signal_id"), signal_id)
    rec = {
        "signal_id": str(signal_id),
        "direction": direction.value,
        "trigger": float(trigger),
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=float(config.E_WAIT_MINUTES))).isoformat(),
        "flag_bar_ts": flag_bar_dt.isoformat() if flag_bar_dt is not None else None,
        "confirm_bar_ts": confirm_bar_dt.isoformat() if confirm_bar_dt is not None else None,
        "trading_date": now.astimezone(config.KST).strftime("%Y%m%d"),
        "session": session,
        "slot_number": (None if slot_number is None else int(slot_number)),
        # 승인 시점 게이트 스냅샷 — 돌파 체결은 게이트를 **다시 평가하지 않고**
        # 이 값을 재생한다. 승인은 T+3 에 이미 끝났고, 15분 뒤 재평가하면 그
        # 사이 바뀐 시장상태가 이미 승인된 신호를 조용히 바꿔 버리기 때문이다.
        # 체결 직전에 다시 보는 것은 hard safety(예산/포지션/중복주문/장시간/
        # 킬스위치)뿐이다. 구조는 pending retry(_tw2_3slot_retry_ctx)와 같다.
        "gate": (dict(gate) if isinstance(gate, dict) else None),
        "fired": False,
    }
    state.e_pending = rec
    return rec


def clear_pending(state, reason: str) -> Optional[dict]:
    """대기를 해제한다. 해제된 레코드를 돌려준다(진단/원장용)."""
    rec = pending_record(state)
    state.e_pending = None
    if rec is not None:
        state.e_last_pending_result = str(reason)
        logger.info("[MACD2][E] pending cleared %s reason=%s",
                    rec.get("signal_id"), reason)
    return rec


def pending_expiry_reason(rec: dict, now: datetime) -> Optional[str]:
    """지금 폐기해야 하는가 — 사유 또는 None."""
    if not rec:
        return None
    date = str(rec.get("trading_date") or "")
    if date and now.astimezone(config.KST).strftime("%Y%m%d") != date:
        return PENDING_EXPIRED_DATE_ROLLOVER
    exp = rec.get("expires_at")
    if exp:
        try:
            if now > datetime.fromisoformat(str(exp)):
                return PENDING_EXPIRED
        except ValueError:
            return PENDING_EXPIRED
    return None


def breakout_hit(rec: dict, price: Optional[float]) -> bool:
    """현재가가 trigger 를 돌파했는가."""
    if not rec or price is None:
        return False
    trig = float(rec.get("trigger") or 0.0)
    if trig <= 0:
        return False
    return (price >= trig) if str(rec.get("direction")) == Direction.UP_RED.value else (price <= trig)


def pending_direction(rec: dict) -> Optional[Direction]:
    try:
        return Direction(str(rec.get("direction")))
    except (ValueError, TypeError):
        return None


def breakout_signal_id(rec: dict) -> str:
    """대기 체결용 signal_id — 승인 시점 id 와 반드시 달라야 한다."""
    return f"{rec.get('signal_id')}{BREAKOUT_SUFFIX}"


# ── ② RS125 ─────────────────────────────────────────────────────────────

def rs_features(closes: np.ndarray, now: datetime) -> Optional[dict]:
    """RS 원자료. **완성봉 종가 배열만** 쓴다(미래정보 없음).

    ``closes`` 는 ``now`` 이전에 완성된 1분봉 종가들(전일 포함)이어야 한다.
    """
    c = np.asarray(closes, dtype=float)
    if c.size < int(config.E_RS_MIN_BARS):
        return None
    ts = pd.Timestamp(now)
    e20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    s = np.sign(c - e20)[-30:]
    s = s[s != 0]
    return {
        "atr60": float(np.abs(np.diff(c[-60:])).mean()) / float(c[-1]) * 100.0,
        "min_open": float(ts.hour * 60 + ts.minute - 540),
        "xc30": float(int((np.diff(s) != 0).sum()) if len(s) > 1 else 0),
    }


def _table_from_path() -> dict:
    """parity 검증 전용 사전계산 표. 지정이 없으면 빈 dict."""
    path = str(getattr(config, "E_RS_TABLE_PATH", "") or "")
    if not path:
        return {}
    cached = getattr(_table_from_path, "_cache", None)
    if cached is not None and cached[0] == path:
        return cached[1]
    try:
        tbl = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        logger.warning("[MACD2][E] RS 표를 읽지 못했다 (%s) -- 라이브 집계로 떨어진다: %s",
                       path, exc)
        tbl = {}
    _table_from_path._cache = (path, tbl)
    return tbl


def rs_samples(state) -> list:
    raw = getattr(state, "e_rs_samples", None)
    return list(raw) if isinstance(raw, list) else []


def note_rs_sample(state, day: str, features: dict) -> None:
    """승인 시점 원자료를 적립한다 -- 다음 영업일부터 통계에 들어간다.

    오늘 표본은 :func:`rs_stats_for_day` 가 'day < 오늘' 로 잘라내므로 오늘
    판정에는 절대 쓰이지 않는다(미래정보 차단)."""
    if not features:
        return
    rows = rs_samples(state)
    rows.append({"day": str(day), **{k: float(features[k]) for k in RS_COLUMNS}})
    limit = int(config.E_RS_SAMPLE_LIMIT)
    if limit > 0 and len(rows) > limit:
        rows = rows[-limit:]
    state.e_rs_samples = rows


def rs_stats_for_day(state, day: str) -> Optional[dict]:
    """``day`` **이전** 표본만으로 만든 z통계와 상위20% 임계.

    사전계산 표(``config.E_RS_TABLE_PATH``)가 있으면 그것을 우선한다.
    """
    tbl = _table_from_path()
    if tbl:
        row = tbl.get(str(day))
        return dict(row) if row else None
    # **expanding window 고정** (2026-10-05 확정). 그 날 이전 전체 진입을 쓰고
    # rolling window 는 쓰지 않는다 -- 벤치마크 +18,412,222 가 expanding 으로
    # 산출된 값이라, 창을 바꾸면 임계가 달라져 parity 검증 자체가 성립하지 않는다.
    # 40일 rolling 은 별도 후보전략이며 이 구현에는 포함하지 않는다.
    rows = [r for r in rs_samples(state) if str(r.get("day", "")) < str(day)]
    if len(rows) < int(config.E_RS_MIN_SAMPLES):
        return None
    frame = pd.DataFrame(rows)
    mu = {c: float(frame[c].mean()) for c in RS_COLUMNS}
    sd = {c: float(frame[c].std() or 1.0) for c in RS_COLUMNS}
    z = sum(RS_SIGNS[c] * (frame[c] - mu[c]) / sd[c] for c in RS_COLUMNS)
    return {"mu": mu, "sd": sd,
            "thr": float(np.quantile(z, float(config.E_RS_PERCENTILE))),
            "n": int(len(frame))}


def rs_score(features: dict, stats: dict) -> Optional[float]:
    if not features or not stats:
        return None
    mu, sd = stats.get("mu") or {}, stats.get("sd") or {}
    try:
        return float(sum(RS_SIGNS[c] * (float(features[c]) - float(mu[c])) / float(sd[c] or 1.0)
                         for c in RS_COLUMNS))
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_rs(state, *, features: Optional[dict], day: str) -> RsDecision:
    """이 진입에 RS 증액을 적용할지. state 를 갱신하지 않는다(순수)."""
    if not is_active(state):
        return NEUTRAL_RS
    if not bool(getattr(config, "E_RS_ENABLED", True)):
        return RsDecision(active=False, hit=False, score=None, threshold=None,
                          samples=0, multiplier=1.0, reason="RS_DISABLED")
    if not features:
        return RsDecision(active=True, hit=False, score=None, threshold=None,
                          samples=0, multiplier=1.0, reason="NO_FEATURES")
    stats = rs_stats_for_day(state, day)
    if not stats:
        return RsDecision(active=True, hit=False, score=None, threshold=None,
                          samples=0, multiplier=1.0, reason="NO_STATS")
    score = rs_score(features, stats)
    thr = float(stats.get("thr"))
    if score is None:
        return RsDecision(active=True, hit=False, score=None, threshold=thr,
                          samples=int(stats.get("n", 0)), multiplier=1.0, reason="NO_SCORE")
    hit = bool(score >= thr)
    return RsDecision(
        active=True, hit=hit, score=round(float(score), 6), threshold=round(thr, 6),
        samples=int(stats.get("n", 0)),
        multiplier=(float(config.E_RS_MULT) if hit else 1.0),
        reason=("RS_HIT" if hit else "RS_MISS"),
    )


# ── 일일 한도 안에서의 증액 ──────────────────────────────────────────────

@dataclass(frozen=True)
class BoostResult:
    """``applied`` 를 budget 배수로 쓰고, ``extra`` 를 누계에 더한다."""
    applied: float
    extra: float
    capped: bool
    base: float
    wanted: float


def apply_rs_boost(state, base_multiplier: float, rs: RsDecision) -> BoostResult:
    """RS 증액을 **일일 잔여 한도 안에서만** 적용한다.

    ``position_sizing.note_entry`` 는 '증액 전' 배수만 기록하므로, 증액분
    (``extra``)은 :func:`note_boost` 가 체결 후 누계에 직접 더해야 한다.
    그러지 않으면 뒤 거래가 여유를 과대평가해 일일한도를 넘는다
    (2026-10-05 연구에서 8일 초과 실측).
    """
    from app.trading.macd2 import position_sizing

    base = float(base_multiplier or 0.0)
    if not rs.hit or base <= 0:
        return BoostResult(applied=base, extra=0.0, capped=False, base=base, wanted=base)
    wanted = base * float(rs.multiplier)
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - position_sizing.exposure_used(state)
    applied = min(wanted, max(room, 0.0))
    return BoostResult(applied=applied, extra=max(applied - base, 0.0),
                       capped=bool(wanted > room + 1e-9), base=base, wanted=wanted)


def note_boost(state, boost: BoostResult) -> None:
    """진입이 **체결된 뒤** 호출 — 증액분을 일일 누계에 더한다."""
    if boost is None or boost.extra <= 0:
        return
    from app.trading.macd2 import position_sizing

    state.x2lite_exposure_used_today = round(
        position_sizing.exposure_used(state) + float(boost.extra), 6)


def reset_daily(state) -> None:
    """날짜 rollover — 대기주문을 폐기한다. RS 표본은 **유지**한다(과거자료)."""
    if pending_record(state) is not None:
        clear_pending(state, PENDING_EXPIRED_DATE_ROLLOVER)
    state.e_last_pending_result = None


def describe(state) -> str:
    """UI/로그용 한 줄 요약."""
    if not is_active(state):
        return "OFF"
    rec = pending_record(state)
    pend = (f"대기 {rec['direction']} trigger {float(rec['trigger']):,.0f} "
            f"~{str(rec.get('expires_at'))[11:16]}" if rec else "대기 없음")
    n = len({str(r.get("day")) for r in rs_samples(state)})
    return (f"EARLY-PASS UP {config.E_EARLY_PASS_DIST_UP_PCT}% / "
            f"DOWN {config.E_EARLY_PASS_DIST_DOWN_PCT}% · 최대 {config.E_WAIT_MINUTES}분 · "
            f"RS x{config.E_RS_MULT} 상위{(1 - config.E_RS_PERCENTILE) * 100:.0f}% "
            f"(표본 {n}일) · {pend}")
