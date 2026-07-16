"""
market_prediction.py

실시간 장세 변화 감지 + 향후 30분/1시간/3시간/내일장 방향 예측.

기존 regime_router(A~F 정적 판단)와 분리된 "동적 예측" 계층이다. 지금은
룰 기반 가중합 점수로 구현하며, 나중에 ML 모델로 교체하기 쉽도록
predict_market_direction()/predict_tomorrow_market() 시그니처를 안정적으로
유지한다 (내부 스코어링 로직만 교체 가능한 구조).

절대 수익을 보장하지 않는다 — 조기경보/확률 추정 도구다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market import regime_features as rf
from app.utils.data_paths import LOGS_DIR

HORIZONS = ("30m", "1h", "3h")

_ROOT = Path(__file__).resolve().parents[2]
_DEBUG_LOG_DIR = LOGS_DIR / "market_prediction_debug"

# 각 horizon의 "전체 시장(overall_market)" 하락압력 구성요소 가중치 (합계 100).
# 반도체 대장주 VWAP(semi_vwap)/주도섹터 회전(theme)은 여기서 제외했다 — 전체
# 시장 판단에 반도체 개별 종목이나 주도테마 유지여부가 섞여 "테마는 유지되니
# 전체 시장도 UP"처럼 오판되는 것을 막기 위함이다(반도체/주도테마는 각각
# predict_semiconductor_all_horizons()/predict_leading_theme_status()로 분리).
# 컴포넌트가 데이터 부재로 None이면 해당 항목을 제외하고 나머지 가중치로
# 재정규화한다.
_HORIZON_WEIGHTS = {
    "30m": {"kospi200_futures": 30.0, "foreign": 30.0, "fx": 20.0, "breadth": 10.0, "nasdaq_futures": 10.0},
    "1h":  {"foreign": 30.0, "kospi200_futures": 25.0, "breadth": 20.0, "fx": 15.0, "nasdaq_futures": 10.0},
    "3h":  {"foreign": 30.0, "breadth": 25.0, "kospi200_futures": 20.0, "fx": 15.0, "news": 10.0},
}

# 반도체(semiconductor_prediction) 전용 가중치 — compute_semiconductor_collapse_score()가
# 이미 VWAP/한미반도체/섹터breadth/미국반도체/수급을 종합하므로 그 값을 그대로
# 쓰되, horizon별로 미국 반도체(us_semi)/국내 VWAP 비중만 다르게 섞는다.
_SEMI_HORIZON_WEIGHTS = {
    "30m": {"semi_vwap": 45.0, "us_semi": 35.0, "nasdaq_futures": 20.0},
    "1h":  {"semi_vwap": 40.0, "us_semi": 35.0, "nasdaq_futures": 15.0, "foreign": 10.0},
    "3h":  {"semi_vwap": 35.0, "us_semi": 30.0, "foreign": 20.0, "nasdaq_futures": 15.0},
}

_COMPONENT_LABELS = {
    "kospi200_futures": "KOSPI200 선물 약세",
    "nasdaq_futures": "나스닥 선물 약세",
    "foreign": "외국인 수급 반전(프록시)",
    "fx": "원/달러 환율 급등",
    "semi_vwap": "반도체 대장주 VWAP 이탈",
    "us_semi": "미국 반도체(MU/NVDA/SOX) 약세",
    "breadth": "상승/하락 종목수 악화",
    "theme": "주도섹터 회전/붕괴",
    "news": "부정적 뉴스 모멘텀",
}


def _log_debug_entry(entry: dict, date_str: Optional[str] = None) -> None:
    """logs/market_prediction_debug/YYYYMMDD.jsonl 에 horizon별 판단 근거 원시값을 남긴다.

    운영 판단에는 쓰지 않는 진단 전용 로그다 — raw_probability, direction_margin,
    각종 score의 cap 전/후 값, guard_rules_applied를 남겨 "왜 이 방향/규모로
    판단했는지"를 사후에 추적할 수 있게 한다.
    """
    try:
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        _DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _DEBUG_LOG_DIR / f"{date_str}.jsonl"
        entry = dict(entry)
        entry.setdefault("logged_at", datetime.now().isoformat(timespec="seconds"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("[MarketPrediction] debug 로그 저장 실패: %s", exc)


def _futures_component_score(snapshot: dict, field: str) -> Optional[float]:
    deltas = snapshot.get("deltas", {})
    d5 = (deltas.get("5m", {}) or {}).get(field)
    d15 = (deltas.get("15m", {}) or {}).get(field)
    parts = []
    if d5 is not None:
        parts.append((rf._norm(-d5, 0.3), 0.6))
    if d15 is not None:
        parts.append((rf._norm(-d15, 0.6), 0.4))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / total_w


def _us_semi_component_score(snapshot: dict) -> Optional[float]:
    """MU/NVDA/SOX 정규장 변화율 기반 미국 반도체 약세 점수(0~100, 높을수록 하락압력)."""
    overseas = snapshot.get("overseas", {}) or {}
    parts = []
    for key, scale in (("micron", 2.5), ("nvidia", 1.5), ("sox", 2.0)):
        node = overseas.get(key) or {}
        if node.get("success") and node.get("change_rate") is not None:
            parts.append(rf._norm(-node["change_rate"], scale))
    if not parts:
        return None
    return sum(parts) / len(parts)


def _build_components(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    """예측에 쓰는 컴포넌트 점수(0~100, 높을수록 하락압력)를 모두 계산한다.

    semi_vwap/theme/us_semi는 overall_market 가중치에는 포함되지 않고
    predict_semiconductor_all_horizons()/predict_leading_theme_status()에서
    각각 사용한다 — 이 함수는 두 축이 공유하는 계산 레지스트리 역할만 한다.
    """
    return {
        "kospi200_futures": _futures_component_score(snapshot, "kospi200_futures_change_rate"),
        "nasdaq_futures": _futures_component_score(snapshot, "nasdaq_futures_change_rate"),
        "foreign": rf.compute_foreign_flow_reversal_score(snapshot),
        "fx": rf.compute_fx_risk_score(snapshot),
        "semi_vwap": 100.0 - rf.compute_semiconductor_leadership_score(snapshot),
        "us_semi": _us_semi_component_score(snapshot),
        "breadth": rf.compute_breadth_deterioration_score(snapshot),
        "theme": rf.compute_theme_rotation_score(snapshot, ref_0920),
        "news": rf.compute_news_shock_score(snapshot),
    }


def _weighted_pressure(components: dict, weights: dict) -> tuple[float, dict]:
    """가중합 하락압력(0~100) + 실제 반영된 컴포넌트별 기여도를 반환한다."""
    used = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for key, w in weights.items():
        v = components.get(key)
        if v is None:
            continue
        weighted_sum += v * w
        weight_total += w
        used[key] = v
    pressure = (weighted_sum / weight_total) if weight_total > 0 else 50.0
    return round(max(0.0, min(100.0, pressure)), 2), used


def _pressure_to_probabilities(down_pressure: float) -> tuple[float, float, float]:
    """down_pressure(0~100, 50=중립) -> (p_down, p_sideways, p_up), 합계=100."""
    d = max(0.0, min(100.0, down_pressure))
    bias = d - 50.0  # -50(강한 상승압력) ~ +50(강한 하락압력)
    p_down = max(2.0, min(90.0, 33.34 + bias * 1.1))
    p_up = max(2.0, min(90.0, 33.33 - bias * 1.1))
    p_sideways = max(2.0, 100.0 - p_down - p_up)
    total = p_down + p_sideways + p_up
    p_down, p_sideways, p_up = (round(x / total * 100, 1) for x in (p_down, p_sideways, p_up))
    return p_down, p_sideways, p_up


# horizon별 방향 판정 엄격도. 30m은 상대적으로 관대하고, 1h는 기본, 3h는
# 가장 보수적이다(짧은 horizon일수록 노이즈가 커서 작은 확률차로도 방향을
# 내는게 실용적이지만, 긴 horizon은 확률차가 작을 때 단정하면 안 된다).
#   min_max          : 1위 확률이 최소 이 값 이상이어야 방향을 낸다(아니면 UNCERTAIN)
#   min_margin_other : 1위와 반대방향 확률의 차이가 이 값 이상이어야 한다
#   min_margin_side   : 1위와 SIDEWAYS 확률의 차이가 이 값 이상이어야 한다
_DIRECTION_THRESHOLDS = {
    "30m": {"min_max": 42.0, "min_margin_other": 10.0, "min_margin_side": 6.0},
    "1h":  {"min_max": 45.0, "min_margin_other": 12.0, "min_margin_side": 8.0},
    "3h":  {"min_max": 48.0, "min_margin_other": 15.0, "min_margin_side": 10.0},
}


def _direction_from_probs(p_down: float, p_sideways: float, p_up: float, horizon: str = "1h") -> str:
    """
    단순 argmax(가장 큰 확률 = 방향)의 문제: up36/down31/side33처럼 3개 확률이
    비슷하게 몰려 있어도 항상 UP/DOWN 중 하나를 "확정적으로" 뱉는다. 실제로는
    이런 경우 "방향을 모른다"가 정확한 답이다.

    margin-threshold 방식: 1위 확률의 절대 크기(min_max)와 2위/SIDEWAYS와의
    차이(margin)가 모두 충분해야만 UP/DOWN을 낸다. 못 미치면 SIDEWAYS 또는
    (1위 확률 자체가 낮으면) UNCERTAIN을 낸다.
    """
    th = _DIRECTION_THRESHOLDS.get(horizon, _DIRECTION_THRESHOLDS["1h"])
    max_prob = max(p_up, p_down, p_sideways)

    if max_prob < th["min_max"]:
        return "UNCERTAIN"
    if abs(p_up - p_down) < 10.0:
        return "SIDEWAYS"
    if abs(p_up - p_sideways) < th["min_margin_side"] or abs(p_down - p_sideways) < th["min_margin_side"]:
        return "SIDEWAYS"
    if p_up >= th["min_max"] and (p_up - p_down) >= th["min_margin_other"] and (p_up - p_sideways) >= th["min_margin_side"]:
        return "UP"
    if p_down >= th["min_max"] and (p_down - p_up) >= th["min_margin_other"] and (p_down - p_sideways) >= th["min_margin_side"]:
        return "DOWN"
    return "SIDEWAYS"


def _direction_margin(p_down: float, p_sideways: float, p_up: float) -> float:
    """1위 확률과 2위 확률의 차이(pp) — debug 로그/신뢰도 참고용."""
    ranked = sorted([p_up, p_down, p_sideways], reverse=True)
    return round(ranked[0] - ranked[1], 2)


_HORIZON_REVERSION_WEIGHT = {"30m": 0.10, "1h": 0.35, "3h": 0.60}


def _apply_recovery_adjustment(
    down_pressure: float, horizon: str,
    recovery_info: Optional[dict], score_deltas: Optional[dict],
) -> tuple[float, list[str]]:
    """
    "위험 국면은 계속된다"는 관성 편향을 줄이기 위해, recovery_score와
    위험점수의 5분/15분 변화 방향(절대값이 아니라 추세)을 down_pressure에 반영한다.

    30분 예측에는 약하게(현재 상태 비중 유지), 3시간 예측에는 강하게 적용한다 —
    "3시간 예측은 current_regime보다 변화율을 더 본다"는 원칙을 구현한 것이다.
    """
    reasons: list[str] = []
    reversion_w = _HORIZON_REVERSION_WEIGHT.get(horizon, 0.0)
    if reversion_w <= 0:
        return down_pressure, reasons

    adjustment = 0.0
    recovery_score = (recovery_info or {}).get("recovery_score")
    if recovery_score is not None and recovery_score > 50.0:
        adjustment -= (recovery_score - 50.0) * reversion_w
        if recovery_score >= 65.0:
            reasons.append(f"회복 신호(recovery_score {recovery_score:.0f}) 반영 — 위험 지속 가정 완화")

    momentum = (score_deltas or {}).get("regime_transition_momentum")
    if momentum is not None:
        # momentum > 0 = 최근 15분간 위험점수 완화 중 -> down_pressure 추가 완화
        adjustment -= momentum * reversion_w * 0.6
        if abs(momentum) >= 8.0:
            trend = "완화" if momentum > 0 else "악화"
            reasons.append(f"최근 위험점수 {trend} 추세(모멘텀 {momentum:+.1f}) 반영")

    adjusted = max(0.0, min(100.0, down_pressure + adjustment))
    return round(adjusted, 2), reasons


_REGIME_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 3}


def _regime_from_pressure(down_pressure: float, market_collapse_score: Optional[float]) -> str:
    """down_pressure(0~100) 구간을 "가드 없이 순수 압력만 봤을 때" 예상되는 regime으로 매핑한다."""
    if down_pressure >= 70.0:
        return "E" if (market_collapse_score is not None and market_collapse_score >= 80.0) else "D"
    if down_pressure >= 55.0:
        return "D"
    if down_pressure <= 20.0:
        return "A"
    if down_pressure <= 35.0:
        return "B"
    return "C"


def _guard_d_to_c(ctx: dict) -> bool:
    """D→C 완화: recovery_score + collapse 완화 추세/낮은 절대값 + VWAP 회복 + 지수/선물 저점 회복 + 데이터품질."""
    rs = ctx.get("recovery_score")
    if rs is None or rs < 60.0:
        return False
    mc = ctx.get("market_collapse_score")
    mc15 = ctx.get("market_collapse_delta_15m")
    collapse_ok = (mc15 is not None and mc15 <= -5.0) or (mc is not None and mc < 55.0)
    if not collapse_ok:
        return False
    if not (ctx.get("hynix_vwap_recovered") or ctx.get("samsung_vwap_recovered")):
        return False
    if not (ctx.get("futures_recovered") or ctx.get("breadth_recovering")):
        return False
    dq = ctx.get("data_quality_score")
    if dq is None or dq < 65.0:
        return False
    return True


def _guard_e_to_d(ctx: dict) -> bool:
    """E→D 완화: recovery_score 최소 조건 + collapse_score가 더 나빠지고 있지 않음 + 절대값<70."""
    rs = ctx.get("recovery_score")
    if rs is None or rs < 45.0:
        return False
    if ctx.get("collapse_rising"):
        return False
    mc = ctx.get("market_collapse_score")
    if mc is None or mc >= 70.0:
        return False
    return True


def _guard_e_to_c(ctx: dict) -> bool:
    """E→C 완화: D→C보다 더 엄격 — recovery_score/하락폭/반도체/VWAP/breadth/데이터품질 모두 충족."""
    rs = ctx.get("recovery_score")
    if rs is None or rs < 70.0:
        return False
    mc15 = ctx.get("market_collapse_delta_15m")
    if mc15 is None or mc15 > -10.0:
        return False
    sc = ctx.get("semiconductor_collapse_score")
    if sc is not None and sc >= 70.0:
        return False
    if not (ctx.get("hynix_vwap_recovered") or ctx.get("samsung_vwap_recovered")):
        return False
    if not ctx.get("breadth_recovering"):
        return False
    dq = ctx.get("data_quality_score")
    if dq is None or dq < 70.0:
        return False
    return True


def _guard_de_to_ab(ctx: dict) -> bool:
    """D/E→A/B: 가장 엄격 — 매우 강한 회복 + 낮은 시장/반도체 붕괴점수 + 선물 반등 확인 + 수급 비-프록시."""
    rs = ctx.get("recovery_score")
    if rs is None or rs < 80.0:
        return False
    mc = ctx.get("market_collapse_score")
    if mc is None or mc >= 50.0:
        return False
    sc = ctx.get("semiconductor_collapse_score")
    if sc is not None and sc >= 60.0:
        return False
    if not ctx.get("futures_recovered"):
        return False
    if ctx.get("foreign_flow_is_proxy", True):
        return False
    return True


def _expected_regime(
    direction: str, down_pressure: float, market_collapse_score: float, current_regime: str,
    recovery_score: Optional[float] = None, collapse_declining: bool = False,
    guard_context: Optional[dict] = None,
) -> tuple[str, list]:
    """
    direction(3분류)이 아니라 down_pressure 자체를 구간화해서 판단한다(원래 방식
    유지). 다만 current_regime이 D/E일 때는 "이번 down_pressure 구간이 시사하는
    더 나은 regime"으로의 완화를 다중 조건 가드(§3)를 통과해야만 허용한다 —
    recovery_score 하나만으로, 또는 한번의 낮은 down_pressure 판독만으로
    즉시 완화되던 기존 동작(근거 없는 낙관적 완화)을 막는 것이 이번 수정의
    핵심이다. 악화(더 나쁜 regime으로 전환)는 위험을 즉시 인지해야 하므로
    가드 없이 바로 허용한다 — 가드는 "완화"에만 걸린다.

    guard_context 키(모두 선택, 없으면 해당 조건은 미충족으로 처리해
    완화를 막는다 — "데이터 부족 시 완화보다 보수적 유지"):
      market_collapse_delta_15m, collapse_rising, semiconductor_collapse_score,
      hynix_vwap_recovered, samsung_vwap_recovered, futures_recovered,
      breadth_recovering, data_quality_score, foreign_flow_is_proxy.

    Returns
    -------
    (regime, guard_rules_applied) — guard_rules_applied는 실제로 완화를
    허용/차단한 가드 이름 목록(디버그 로그용, 빈 리스트일 수 있음).
    """
    guard_context = guard_context or {}
    cur = current_regime if current_regime in _REGIME_ORDER else "F"
    ctx = {
        "recovery_score": recovery_score,
        "collapse_declining": collapse_declining,
        "market_collapse_score": market_collapse_score,
        "market_collapse_delta_15m": guard_context.get("market_collapse_delta_15m"),
        "collapse_rising": guard_context.get("collapse_rising", False),
        "semiconductor_collapse_score": guard_context.get("semiconductor_collapse_score"),
        "hynix_vwap_recovered": guard_context.get("hynix_vwap_recovered"),
        "samsung_vwap_recovered": guard_context.get("samsung_vwap_recovered"),
        "futures_recovered": guard_context.get("futures_recovered"),
        "breadth_recovering": guard_context.get("breadth_recovering"),
        "data_quality_score": guard_context.get("data_quality_score"),
        "foreign_flow_is_proxy": guard_context.get("foreign_flow_is_proxy", True),
    }

    def _relax_from_de(ab_target: Optional[str]) -> tuple[str, list]:
        """cur가 D/E일 때 완화 후보를 가드로 검증한다. ab_target이 주어지면
        (down_pressure가 그만큼 낮다는 뜻) 가장 엄격한 D/E->A/B 가드부터 시도한다."""
        if ab_target is not None and _guard_de_to_ab(ctx):
            return ab_target, [f"{cur}_TO_{ab_target}_PASS"]
        if cur == "E":
            if _guard_e_to_c(ctx):
                return "C", ["E_TO_C_PASS"]
            if _guard_e_to_d(ctx):
                return "D", ["E_TO_D_PASS"]
            return cur, ["E_RELAXATION_BLOCKED"]
        if _guard_d_to_c(ctx):
            return "C", ["D_TO_C_PASS"]
        return cur, ["D_RELAXATION_BLOCKED"]

    if down_pressure >= 70.0:
        raw = "E" if (market_collapse_score is not None and market_collapse_score >= 80.0) else "D"
        if cur not in ("D", "E") or _REGIME_ORDER.get(raw, 3) >= _REGIME_ORDER.get(cur, 3):
            return raw, []
        return _relax_from_de(ab_target=None)

    if down_pressure >= 55.0:
        if cur in ("D", "E"):
            return _relax_from_de(ab_target=None)
        return "D", []

    if down_pressure <= 20.0:
        if cur in ("D", "E"):
            return _relax_from_de(ab_target="A")
        return "A", []

    if down_pressure <= 35.0:
        if cur in ("D", "E"):
            return _relax_from_de(ab_target="B")
        return "A", []

    # 35 < down_pressure < 55 (raw 후보는 C)
    if cur in ("D", "E"):
        return _relax_from_de(ab_target=None)
    return "C", []


def _vwap_recovered(snapshot: dict, key: str) -> Optional[bool]:
    """price>=vwap이면 회복(True). 가격/VWAP 중 하나라도 없으면 None(판단 불가)."""
    node = snapshot.get("domestic", {}).get(key, {}) or {}
    price, vwap = node.get("current_price"), node.get("vwap")
    if price is None or vwap is None or vwap == 0:
        return None
    return bool(price >= vwap)


def _futures_recovered_from_low(snapshot: dict) -> Optional[bool]:
    """KOSPI200 선물의 최근 5분 변화가 15분 변화보다 덜 나쁘면(반등 중) True로 본다."""
    deltas = snapshot.get("deltas", {}) or {}
    d5 = (deltas.get("5m", {}) or {}).get("kospi200_futures_change_rate")
    d15 = (deltas.get("15m", {}) or {}).get("kospi200_futures_change_rate")
    if d5 is None:
        return None
    if d15 is None:
        return bool(d5 > -0.05)
    return bool(d5 > d15)


def _foreign_flow_is_proxy(snapshot: dict) -> bool:
    flow = snapshot.get("domestic", {}).get("investor_flow_market", {}) or {}
    return (not flow.get("success")) or flow.get("is_proxy", True)


def _breadth_recovering(snapshot: dict) -> Optional[bool]:
    """상승/하락종목수가 0/0이 아니고 상승>=하락이면 회복 중으로 본다(근사치)."""
    domestic = snapshot.get("domestic", {}) or {}
    adv, dec = domestic.get("advancers"), domestic.get("decliners")
    if not adv and not dec:
        return None
    return bool((adv or 0) >= (dec or 0))


def _build_guard_context(snapshot: dict, data_quality_score: Optional[float], semiconductor_collapse_score: Optional[float]) -> dict:
    """_expected_regime()에 넘길 guard_context를 snapshot에서 조립한다."""
    return {
        "semiconductor_collapse_score": semiconductor_collapse_score,
        "hynix_vwap_recovered": _vwap_recovered(snapshot, "hynix"),
        "samsung_vwap_recovered": _vwap_recovered(snapshot, "samsung"),
        "futures_recovered": _futures_recovered_from_low(snapshot),
        "breadth_recovering": _breadth_recovering(snapshot),
        "data_quality_score": data_quality_score,
        "foreign_flow_is_proxy": _foreign_flow_is_proxy(snapshot),
    }


def predict_market_direction(
    horizon: str,
    snapshot: dict,
    regime_result: Optional[dict] = None,
    ref_0920: Optional[dict] = None,
    recovery_info: Optional[dict] = None,
    score_deltas: Optional[dict] = None,
) -> dict:
    """
    향후 30분/1시간/3시간 국내증시 전체(overall_market) 방향을 확률로 예측한다.
    반도체 개별 판단은 predict_semiconductor_all_horizons()를 따로 쓴다.

    Parameters
    ----------
    horizon : "30m" | "1h" | "3h"
    snapshot : market_data_collector.collect() 결과 (deltas 포함)
    regime_result : MarketRegimeRouter.determine_regime() 결과 (선택 — 있으면
                    market_collapse_score/현재 regime을 재사용)
    ref_0920 : 09:20 기준 스냅샷 (theme_rotation_score 계산에 사용)
    recovery_info : regime_features.compute_recovery_score() 결과 (선택) —
                    "위험 지속" 관성 편향을 줄이기 위한 회복 신호.
    score_deltas : regime_router가 계산한 위험/회복 점수의 5분/15분 변화량
                   + regime_transition_momentum (선택).

    Returns
    -------
    dict: horizon, direction, probability_up/sideways/down, expected_regime,
          confidence_score, key_reasons, risk_flags, components, recovery_score,
          down_pressure_score_raw
    """
    if horizon not in _HORIZON_WEIGHTS:
        raise ValueError(f"지원하지 않는 horizon: {horizon} (30m/1h/3h만 가능)")

    regime_result = regime_result or {}
    scores = regime_result.get("scores", {}) or {}
    market_collapse_score = scores.get("market_collapse_score")
    if market_collapse_score is None:
        market_collapse_score = rf.compute_market_collapse_score(snapshot, ref_0920)
    semiconductor_collapse_score = scores.get("semiconductor_collapse_score")
    if semiconductor_collapse_score is None:
        semiconductor_collapse_score = rf.compute_semiconductor_collapse_score(snapshot)
    current_regime = regime_result.get("regime", "F")
    data_quality_score = regime_result.get("data_quality_score")
    if data_quality_score is None:
        data_quality_score = rf.compute_data_quality_score(snapshot)

    components = _build_components(snapshot, ref_0920)
    weights = _HORIZON_WEIGHTS[horizon]
    down_pressure_raw, used = _weighted_pressure(components, weights)
    down_pressure, recovery_reasons = _apply_recovery_adjustment(
        down_pressure_raw, horizon, recovery_info, score_deltas,
    )

    collapse_delta_15m = (score_deltas or {}).get("market_collapse_score_delta_15m")
    collapse_declining = collapse_delta_15m is not None and collapse_delta_15m <= -10.0
    collapse_rising = collapse_delta_15m is not None and collapse_delta_15m >= 5.0
    recovery_score = (recovery_info or {}).get("recovery_score")

    p_down_before_guard, p_sideways_before_guard, p_up_before_guard = _pressure_to_probabilities(down_pressure)
    direction = _direction_from_probs(p_down_before_guard, p_sideways_before_guard, p_up_before_guard, horizon)
    p_down, p_sideways, p_up = p_down_before_guard, p_sideways_before_guard, p_up_before_guard
    direction_margin = _direction_margin(p_down, p_sideways, p_up)

    guard_context = _build_guard_context(snapshot, data_quality_score, semiconductor_collapse_score)
    guard_context["collapse_rising"] = collapse_rising
    expected_regime_before_guard = _regime_from_pressure(down_pressure, market_collapse_score)
    expected_regime, guard_rules_applied = _expected_regime(
        direction, down_pressure, market_collapse_score, current_regime,
        recovery_score=recovery_score, collapse_declining=collapse_declining,
        guard_context=guard_context,
    )

    # 신뢰도: 데이터 완결성(가중치 커버리지) x 판단의 뚜렷함(중립 50에서 얼마나 떨어졌는지).
    # 예측 방향과 recovery_score가 정면 충돌하면(예: DOWN인데 recovery_score 높음) 감점한다.
    coverage = (sum(used.values()) and len(used) / len(weights)) or 0.0
    decisiveness = min(1.0, abs(down_pressure - 50.0) / 40.0)
    confidence_before_cap = 40.0 * coverage + 55.0 * decisiveness + 5.0
    if recovery_score is not None and direction == "DOWN" and recovery_score >= 70.0:
        confidence_before_cap -= 15.0
    confidence_before_cap = round(max(10.0, min(95.0, confidence_before_cap)), 1)

    # §6: data_quality_score가 낮으면 confidence를 강제로 낮춘다 — "데이터 부족인데
    # confidence를 높게 표시하지 말 것"을 산식이 아니라 하드캡으로 보장한다.
    confidence_score = confidence_before_cap
    if data_quality_score is not None:
        if data_quality_score < 60.0:
            confidence_score = min(confidence_score, 55.0)
        elif data_quality_score < 70.0:
            confidence_score = min(confidence_score, 65.0)
    confidence_score = round(confidence_score, 1)

    ranked = sorted(used.items(), key=lambda kv: abs(kv[1] - 50.0) * weights.get(kv[0], 0), reverse=True)
    key_reasons = [
        f"{_COMPONENT_LABELS.get(k, k)} ({v:.0f}점)"
        for k, v in ranked[:4] if abs(v - 50.0) >= 8.0
    ]
    key_reasons.extend(recovery_reasons)
    if not key_reasons:
        key_reasons = ["뚜렷한 우세 신호 없음 — 중립적 흐름"]

    risk_flags = [
        _COMPONENT_LABELS.get(k, k) for k, v in used.items() if v >= 65.0
    ]

    result = {
        "horizon": horizon,
        "direction": direction,
        "direction_margin": direction_margin,
        "probability_up": p_up,
        "probability_sideways": p_sideways,
        "probability_down": p_down,
        "expected_regime": expected_regime,
        "expected_regime_before_guard": expected_regime_before_guard,
        "guard_rules_applied": guard_rules_applied,
        "confidence_score": confidence_score,
        "confidence_before_cap": confidence_before_cap,
        "data_quality_score": data_quality_score,
        "market_collapse_score": market_collapse_score,
        "semiconductor_collapse_score": semiconductor_collapse_score,
        "key_reasons": key_reasons,
        "risk_flags": risk_flags,
        "recovery_score": recovery_score,
        "down_pressure_score_raw": down_pressure_raw,
        "down_pressure_score": down_pressure,
        "components": used,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    logger.debug(
        "[MarketPrediction] horizon=%s direction=%s p_down=%.1f expected_regime=%s conf=%.1f",
        horizon, direction, p_down, expected_regime, confidence_score,
    )
    _log_debug_entry({
        "horizon": horizon,
        "raw_probability_up": p_up_before_guard,
        "raw_probability_down": p_down_before_guard,
        "raw_probability_sideways": p_sideways_before_guard,
        "final_direction_label": direction,
        "direction_margin": direction_margin,
        "recovery_score": recovery_score,
        "market_collapse_score": market_collapse_score,
        "semiconductor_collapse_score": semiconductor_collapse_score,
        "data_quality_score_before_cap": data_quality_score,
        "data_quality_score_after_cap": data_quality_score,
        "confidence_before_cap": confidence_before_cap,
        "confidence_after_cap": confidence_score,
        "expected_regime_before_guard": expected_regime_before_guard,
        "expected_regime_after_guard": expected_regime,
        "guard_rules_applied": guard_rules_applied,
        "current_regime": current_regime,
    })
    return result


def predict_all_horizons(
    snapshot: dict,
    regime_result: Optional[dict] = None,
    ref_0920: Optional[dict] = None,
    recovery_info: Optional[dict] = None,
    score_deltas: Optional[dict] = None,
) -> dict:
    """30분/1시간/3시간 "전체 시장(overall_market)" 예측을 한번에 계산해 {horizon: result} 로 반환한다.

    반도체/주도테마는 별도 축이므로 predict_semiconductor_all_horizons()/
    predict_leading_theme_status()를 함께 봐야 한다 — 이 함수만으로는 반도체
    상태를 판단하지 않는다(§4).
    """
    return {
        h: predict_market_direction(h, snapshot, regime_result, ref_0920, recovery_info, score_deltas)
        for h in HORIZONS
    }


# ---------------------------------------------------------------------------
# 반도체(semiconductor) 예측 — 전체 시장과 분리된 축 (§4/§5)
# ---------------------------------------------------------------------------

def _all_semi_stocks_below_vwap(snapshot: dict) -> tuple[bool, int]:
    """하이닉스/삼성전자/한미반도체 중 VWAP 판단 가능한 종목들이 "전부" VWAP 아래인지.

    Returns (all_below, checked_count) — checked_count<2면 데이터가 부족해
    "전부 이탈"이라고 단정하지 않는다(all_below=False로 반환).
    """
    statuses = []
    for key in ("hynix", "samsung", "hanmi"):
        recovered = _vwap_recovered(snapshot, key)
        if recovered is not None:
            statuses.append(recovered)
    if len(statuses) < 2:
        return False, len(statuses)
    return all(v is False for v in statuses), len(statuses)


def predict_semiconductor_direction(
    horizon: str,
    snapshot: dict,
    regime_result: Optional[dict] = None,
    recovery_info: Optional[dict] = None,
    mu_data_status: Optional[str] = None,
    mu_data_source: Optional[str] = None,
) -> dict:
    """
    반도체 대장주(하이닉스/삼성전자/한미반도체) + 미국 반도체(MU/NVDA/SOX) 기준
    30분/1시간/3시간 방향을 전체 시장과 별개로 예측한다.

    §5 가드: semiconductor_collapse_score>=75 이거나 3개 대장주가 모두 VWAP
    이탈 상태면 UP을 낼 수 없다(강제로 SIDEWAYS/UNCERTAIN으로 낮춘다).
    MU 데이터가 지연/Yahoo-only/누락이면 confidence를 상한한다.
    recovery_score<50이면 "반도체 반등(C/A급 완화)" 신호로 쓰지 않는다.
    """
    if horizon not in _SEMI_HORIZON_WEIGHTS:
        raise ValueError(f"지원하지 않는 horizon: {horizon} (30m/1h/3h만 가능)")

    regime_result = regime_result or {}
    scores = regime_result.get("scores", {}) or {}
    semiconductor_collapse_score = scores.get("semiconductor_collapse_score")
    if semiconductor_collapse_score is None:
        semiconductor_collapse_score = rf.compute_semiconductor_collapse_score(snapshot)

    components = _build_components(snapshot)
    weights = _SEMI_HORIZON_WEIGHTS[horizon]
    down_pressure, used = _weighted_pressure(components, weights)

    p_down, p_sideways, p_up = _pressure_to_probabilities(down_pressure)
    direction = _direction_from_probs(p_down, p_sideways, p_up, horizon)
    direction_before_guard = direction

    all_below_vwap, vwap_checked_count = _all_semi_stocks_below_vwap(snapshot)
    guard_notes: list[str] = []
    if direction == "UP" and semiconductor_collapse_score is not None and semiconductor_collapse_score >= 75.0:
        direction = "SIDEWAYS" if p_sideways >= 20.0 else "UNCERTAIN"
        guard_notes.append(f"semiconductor_collapse_score {semiconductor_collapse_score:.0f}>=75 — UP 판정 금지")
    elif direction == "UP" and all_below_vwap:
        direction = "SIDEWAYS" if p_sideways >= 20.0 else "UNCERTAIN"
        guard_notes.append("하이닉스/삼성전자/한미반도체 대장주가 모두 VWAP 이탈 — UP 판정 금지")

    coverage = (sum(used.values()) and len(used) / len(weights)) or 0.0
    decisiveness = min(1.0, abs(down_pressure - 50.0) / 40.0)
    confidence_before_cap = round(max(10.0, min(95.0, 40.0 * coverage + 55.0 * decisiveness + 5.0)), 1)
    confidence_score = confidence_before_cap

    mu_delayed = mu_data_status in ("DELAYED", "MISSING") or (mu_data_source == "yahoo")
    if mu_delayed:
        confidence_score = min(confidence_score, 60.0)
        guard_notes.append(f"MU 데이터 상태={mu_data_status or 'UNKNOWN'}(source={mu_data_source}) — 반도체 예측 신뢰도 상한 60 적용")
    if mu_data_status == "MISSING":
        confidence_score = min(confidence_score, 55.0)

    recovery_score = (recovery_info or {}).get("recovery_score")
    rebound_blocked = recovery_score is None or recovery_score < 50.0
    if rebound_blocked and direction == "UP":
        guard_notes.append("recovery_score<50 — 반도체 반등(C/A급 완화) 신호로 사용하지 않음")

    confidence_score = round(confidence_score, 1)

    ranked = sorted(used.items(), key=lambda kv: abs(kv[1] - 50.0) * weights.get(kv[0], 0), reverse=True)
    key_reasons = [f"{_COMPONENT_LABELS.get(k, k)} ({v:.0f}점)" for k, v in ranked[:3] if abs(v - 50.0) >= 8.0]
    key_reasons.extend(guard_notes)
    if not key_reasons:
        key_reasons = ["반도체 대장주/미국반도체 뚜렷한 우세 신호 없음"]

    return {
        "horizon": horizon,
        "direction": direction,
        "direction_before_guard": direction_before_guard,
        "probability_up": p_up,
        "probability_sideways": p_sideways,
        "probability_down": p_down,
        "semiconductor_collapse_score": semiconductor_collapse_score,
        "all_semi_stocks_below_vwap": all_below_vwap,
        "vwap_checked_count": vwap_checked_count,
        "mu_data_status": mu_data_status,
        "mu_data_source": mu_data_source,
        "recovery_score": recovery_score,
        "semiconductor_rebound_blocked": rebound_blocked,
        "confidence_score": confidence_score,
        "confidence_before_cap": confidence_before_cap,
        "down_pressure_score": down_pressure,
        "key_reasons": key_reasons,
        "guard_notes": guard_notes,
        "components": used,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }


def predict_semiconductor_all_horizons(
    snapshot: dict,
    regime_result: Optional[dict] = None,
    recovery_info: Optional[dict] = None,
    mu_data_status: Optional[str] = None,
    mu_data_source: Optional[str] = None,
) -> dict:
    """30분/1시간/3시간 반도체(semiconductor_prediction) 예측을 한번에 계산한다."""
    return {
        h: predict_semiconductor_direction(h, snapshot, regime_result, recovery_info, mu_data_status, mu_data_source)
        for h in HORIZONS
    }


# ---------------------------------------------------------------------------
# 주도테마(leading_theme) 상태 — 전체 시장/반도체와 분리된 축 (§4/§7)
# ---------------------------------------------------------------------------

def predict_leading_theme_status(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    """
    현재 주도섹터(엔터/게임, 방산, 전력기기 등)가 09:20 기준과 비교해 유지되고
    있는지만 보고한다 — 방향/확률을 내지 않는다. "주도테마가 유지된다"는 것과
    "전체 시장/반도체가 회복됐다"는 것은 서로 다른 질문이라는 원칙(§4/§9)을
    구조적으로 강제하기 위해, 이 함수는 절대 UP/DOWN을 반환하지 않는다.

    status: "STABLE"(정상 계산된 안정) | "UNKNOWN"(계산 근거 자체가 없음,
    "안정적"이라는 긍정 신호로 쓰면 안 됨 — §7).
    """
    leading_sectors = rf._leader_sectors(snapshot)
    status = rf.classify_theme_rotation_status(snapshot, ref_0920)
    theme_rotation_score = rf.compute_theme_rotation_score(snapshot, ref_0920)
    leading_theme_maintained = status == "STABLE" and theme_rotation_score <= 55.0

    return {
        "leading_sectors": leading_sectors,
        "status": status,
        "theme_rotation_score": theme_rotation_score,
        "leading_theme_maintained": leading_theme_maintained,
        "disclaimer": "주도테마 유지 여부는 전체 시장·반도체 회복과 다릅니다. "
                      "일부 테마가 유지되어도 전체 시장이나 반도체가 회복된 것은 아닙니다.",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 내일장 예측
# ---------------------------------------------------------------------------

def _tomorrow_state(now_hm: Optional[str] = None) -> str:
    """
    INTRADAY_PRELIMINARY(장중, 단정 금지) / CLOSING_BASED(장마감 후) /
    US_SESSION_UPDATED(다음날 08:50 이전, 미국장 실결과 반영 중) /
    PREOPEN_FINAL(다음날 08:50~09:00, 한국장 개장 직전 최종판단).

    app.models.hynix_price_predictor._tomorrow_state()와 동일한 4단계
    명칭/경계를 쓴다 — 하이닉스 개별종목 예측과 전체 시장 예측이 같은
    "장중 잠정값" 개념을 다른 이름으로 표시해 혼란을 주지 않기 위함이다.
    """
    now_hm = now_hm or datetime.now().strftime("%H:%M")
    if now_hm < "08:50":
        return "US_SESSION_UPDATED"
    if now_hm < "09:00":
        return "PREOPEN_FINAL"
    if now_hm < "15:30":
        return "INTRADAY_PRELIMINARY"
    return "CLOSING_BASED"


_TOMORROW_INTRADAY_DISCLAIMER = (
    "내일장 예측은 장중 잠정값입니다. 장마감 수급 및 미국장 결과 반영 전이라 신뢰도 제한."
)


def _classify_tomorrow_direction(p_down: float, p_sideways: float, p_up: float) -> str:
    """
    STRONG_DOWN/WEAK_DOWN/STRONG_UP/WEAK_UP/SIDEWAYS/UNCERTAIN로 세분화한다.
    예: down40/side33/up27 -> 방향은 DOWN이지만 down<45라서 "약한 DOWN(잠정)"만
    허용하고, "확실한 하락"으로 단정하지 않는다.
    """
    base = _direction_from_probs(p_down, p_sideways, p_up, horizon="1h")
    if base == "DOWN":
        return "STRONG_DOWN" if p_down >= 45.0 else "WEAK_DOWN"
    if base == "UP":
        return "STRONG_UP" if p_up >= 45.0 else "WEAK_UP"
    return base  # SIDEWAYS | UNCERTAIN


def predict_tomorrow_market(
    snapshot: dict,
    regime_history: Optional[list] = None,
    now_hm: Optional[str] = None,
    ref_0920: Optional[dict] = None,
) -> dict:
    """
    내일 한국장 방향을 예측한다 (오늘 하루 흐름 + 미국장/환율/반도체 대장주 기준).

    Returns
    -------
    dict: tomorrow_direction, probability_up/sideways/down, expected_open_gap,
          expected_leading_sector, semiconductor_next_day_bias, risk_level,
          confidence_score, key_reasons, state
    """
    state = _tomorrow_state(now_hm)
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})

    components = _build_components(snapshot, ref_0920)
    market_collapse = rf.compute_market_collapse_score(snapshot, ref_0920)
    semi_collapse = rf.compute_semiconductor_collapse_score(snapshot)

    # regime 변화 이력이 험할수록(악화 방향 전환이 많을수록) 하락 가중
    regime_change_penalty = 0.0
    if regime_history:
        bad = {"D", "E", "F"}
        transitions_to_bad = sum(
            1 for i in range(1, len(regime_history))
            if regime_history[i].get("regime") in bad and regime_history[i - 1].get("regime") not in bad
        )
        regime_change_penalty = min(20.0, transitions_to_bad * 10.0)

    # theme(주도섹터 회전)은 여기서 제외했다 — §4 원칙(주도테마 유지와 전체시장
    # 회복을 혼동하지 않는다)에 따라 내일장 "전체 시장" 예측에도 주도테마를
    # 섞지 않는다. 주도테마 상태는 predict_leading_theme_status()로 별도 확인한다.
    weights = {"foreign": 30.0, "breadth": 20.0, "kospi200_futures": 15.0, "fx": 20.0, "news": 10.0}
    down_pressure, used = _weighted_pressure(components, weights)
    down_pressure = round(min(100.0, down_pressure + regime_change_penalty), 2)

    # 미국 마지막거래일/프리마켓 흐름 반영 (야간 미국장 결과가 다음날 갭에 큰 영향)
    us_bias_components = []
    for key, scale in (("nasdaq", 1.0), ("sox", 2.0)):
        node = overseas.get(key)
        if node and node.get("success"):
            us_bias_components.append(rf._norm(-rf._rate(node), scale))
    last_session = overseas.get("us_last_session", {}) or {}
    for key, scale in (("micron", 3.0), ("nvidia", 3.0)):
        node = last_session.get(key)
        if node and node.get("success") and node.get("change_rate") is not None:
            us_bias_components.append(rf._norm(-node["change_rate"], scale))
    us_bias = sum(us_bias_components) / len(us_bias_components) if us_bias_components else 50.0
    down_pressure = round(min(100.0, max(0.0, down_pressure * 0.7 + us_bias * 0.3)), 2)

    p_down, p_sideways, p_up = _pressure_to_probabilities(down_pressure)
    tomorrow_direction = _classify_tomorrow_direction(p_down, p_sideways, p_up)

    if tomorrow_direction == "STRONG_DOWN" and down_pressure >= 65:
        gap = "GAP_DOWN"
    elif tomorrow_direction == "STRONG_UP" and down_pressure <= 35:
        gap = "GAP_UP"
    elif state == "INTRADAY_PRELIMINARY":
        gap = "FLAT_OR_UNCERTAIN"
    else:
        gap = "FLAT"

    leader_sectors = rf._leader_sectors(snapshot)
    expected_leading_sector = leader_sectors[0] if leader_sectors else "unknown"

    if semi_collapse >= 65:
        semi_bias = "NEGATIVE"
    elif semi_collapse <= 35:
        semi_bias = "POSITIVE"
    else:
        semi_bias = "NEUTRAL"

    if market_collapse >= 80 or semi_collapse >= 80:
        risk_level = "HIGH"
    elif market_collapse >= 60 or semi_collapse >= 60:
        risk_level = "ELEVATED"
    else:
        risk_level = "NORMAL"

    us_status = overseas.get("us_market_status", {}) or {}
    coverage = len(used) / len(weights) if weights else 0.0
    decisiveness = min(1.0, abs(down_pressure - 50.0) / 40.0)
    confidence_before_cap = round(max(10.0, min(90.0, 35.0 * coverage + 45.0 * decisiveness + 10.0)), 1)
    confidence_score = confidence_before_cap
    if us_status.get("is_us_holiday") or us_status.get("is_us_weekend"):
        confidence_score = round(confidence_score * 0.9, 1)  # 휴장으로 미국 데이터 공백 -> 소폭 하향

    # §8: 장중(INTRADAY_PRELIMINARY)에는 장마감 수급/미국장 결과가 아직 반영되지
    # 않았으므로 confidence를 60으로 상한한다 — "장중 잠정값"임을 수치로도 강제한다.
    disclaimer = None
    if state == "INTRADAY_PRELIMINARY":
        confidence_score = min(confidence_score, 60.0)
        disclaimer = _TOMORROW_INTRADAY_DISCLAIMER
    confidence_score = round(confidence_score, 1)

    ranked = sorted(used.items(), key=lambda kv: abs(kv[1] - 50.0) * weights.get(kv[0], 0), reverse=True)
    key_reasons = [f"{_COMPONENT_LABELS.get(k, k)} ({v:.0f}점)" for k, v in ranked[:3] if abs(v - 50.0) >= 8.0]
    if regime_change_penalty > 0:
        key_reasons.append(f"오늘 장중 유형이 악화 방향으로 {int(regime_change_penalty/10)}회 전환")
    if us_bias_components:
        key_reasons.append(f"미국 반도체/나스닥 흐름 반영(추정 {us_bias:.0f}점)")
    if not key_reasons:
        key_reasons = ["뚜렷한 우세 신호 없음"]

    return {
        "tomorrow_direction": tomorrow_direction,
        "probability_up": p_up,
        "probability_sideways": p_sideways,
        "probability_down": p_down,
        "expected_open_gap": gap,
        "expected_leading_sector": expected_leading_sector,
        "semiconductor_next_day_bias": semi_bias,
        "risk_level": risk_level,
        "confidence_score": confidence_score,
        "confidence_before_cap": confidence_before_cap,
        "disclaimer": disclaimer,
        "key_reasons": key_reasons,
        "state": state,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
