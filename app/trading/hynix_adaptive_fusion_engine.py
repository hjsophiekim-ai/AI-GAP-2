"""hynix_adaptive_fusion_engine.py — Prediction AI V2를 실제 mock 주문에 반영하되
ACTIVE_FUSION(현재 수익 중인 전략)을 완전히 대체하지 않는 성과기반 Adaptive Fusion 엔진.

목표는 "매일 수익을 보장"하는 것이 아니라 기대수익(Expected Value)과 Profit Factor를
높이면서 MDD(최대낙폭)를 제한하는 것이다.

구조(5개 모델 → 1개 최종 결정):
  A. ACTIVE_FUSION(app.trading.hynix_active_strategy_engine.decide_active_strategy_action)
  B. Prediction AI V2(app.models.hynix_decision_v2 — 자체 확률 + Adaptive Threshold)
  C. Cycle & Turning Point AI(app.trading.hynix_cycle_detector)
  D. Early Prediction / Momentum Inflection(Cycle AI의 momentum/turning_point를 재사용)
  E. Micron Proxy / External Semiconductor(app.models.micron_proxy_prediction)

각 모델은 표준 스키마(build_model_result)로 변환되고, 모델 상태(LIVE_VALIDATED/
ADVISORY/SHADOW/DEGRADED)에 따라 가중치가 달라진다. 데이터가 없는 모델은 중립값
50을 넣지 않고 완전히 제외한 뒤, 남은 가중치를 100%로 재정규화한다.

이 모듈 자체는 주문을 실행하지 않는다 — decide()의 반환값(FusionDecision)은 "권장"
행동이며, 실제 브로커 호출은 호출부(app/services/hynix_switch_engine.py)가 mock
모드에서만, 명시적 opt-in 토글(state["adaptive_fusion_enabled"])이 켜졌을 때만
수행한다. real 모드/.env/실전 마스터 스위치는 이 모듈에서 절대 다루지 않는다.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_FUSION_CONFIG_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_FUSION_V2_DEFAULTS = {
    "entry_ladder": [
        {"floor": 68.0, "pct": 65.0}, {"floor": 60.0, "pct": 45.0},
        {"floor": 55.0, "pct": 25.0}, {"floor": 52.0, "pct": 10.0},
    ],
    "entry_min_probability": 52.0, "exploratory_max_pct": 10.0,
    "daily_target_trades_min": 4, "daily_target_trades_max": 5,
    "max_daily_round_trips": 6, "same_direction_reentry_cooldown_seconds": 600,
    "time_relief_stage_1_time": "11:00", "time_relief_stage_1_pct": 1.5,
    "time_relief_stage_2_time": "13:00", "time_relief_stage_2_pct": 1.5,
    "time_relief_max_total_pct": 3.0,
    "strong_signal_conflict_threshold": 70.0,
    "disagreement_leader_min_confidence": 75.0, "disagreement_max_opposing_strong": 2,
    "disagreement_opposing_strong_threshold": 65.0, "disagreement_entry_pct": 12.0,
    "active_fusion_neutral_band": 4.0, "active_fusion_neutral_weight_scale": 0.5,
    "consecutive_loss_halve_threshold": 2, "consecutive_loss_block_threshold": 3,
}


def _load_fusion_v2_config() -> dict:
    """config/hynix_enhanced_weights.json의 adaptive_fusion_v2 섹션을 읽는다(요구사항
    9절 — 코드 수정 없이 진입사다리/거래빈도/손실제한 값을 조정할 수 있어야 한다).
    파일/섹션이 없거나 읽기 실패해도 하드코딩된 기본값으로 안전하게 동작한다."""
    try:
        if _FUSION_CONFIG_PATH.exists():
            data = json.loads(_FUSION_CONFIG_PATH.read_text(encoding="utf-8"))
            section = data.get("adaptive_fusion_v2") or {}
            return {**_FUSION_V2_DEFAULTS, **section}
    except Exception as exc:
        logger.debug("[AdaptiveFusion] adaptive_fusion_v2 설정 로드 실패, 기본값 사용: %s", exc)
    return dict(_FUSION_V2_DEFAULTS)


# =============================================================================
# 모델 이름 / 상태 상수
# =============================================================================

MODEL_ACTIVE_FUSION = "ACTIVE_FUSION"
MODEL_PREDICTION_V2 = "PREDICTION_V2"
MODEL_CYCLE_AI = "CYCLE_AI"
MODEL_EARLY_PREDICTION = "EARLY_PREDICTION"
MODEL_MICRON_PROXY = "MICRON_PROXY"

MODEL_STATUS_LIVE_VALIDATED = "LIVE_VALIDATED"
MODEL_STATUS_ADVISORY = "ADVISORY"
MODEL_STATUS_SHADOW = "SHADOW"
MODEL_STATUS_DEGRADED = "DEGRADED"

ACTION_HYNIX = "HYNIX"
ACTION_INVERSE = "INVERSE"
ACTION_HOLD = "HOLD"


def compute_live_hynix_trend(df_1min, now: Optional[datetime] = None) -> dict:
    """Compute the live domestic trend that must dominate Enhanced entries."""
    now = now or datetime.now()
    result = {
        "as_of": now.isoformat(timespec="seconds"), "available": False, "direction": ACTION_HOLD,
        "is_stale": True, "age_minutes": None, "returns": {}, "vwap": None,
        "last_price": None, "above_vwap": False, "ema_slope_pct": None,
        "higher_highs": False, "higher_lows": False, "lower_highs": False, "lower_lows": False,
        "hynix_uptrend_confirmed": False, "hynix_downtrend_confirmed": False,
        "top_factors": [],
    }
    if df_1min is None or getattr(df_1min, "empty", True):
        result["top_factors"].append("hynix 1m data unavailable")
        return result
    try:
        df = df_1min.copy()
        for col in ("close", "high", "low", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        if df.empty:
            result["top_factors"].append("hynix close data unavailable")
            return result
        ts = None
        if "datetime" in df.columns:
            try:
                ts = pd.to_datetime(df["datetime"].iloc[-1]).to_pydatetime()
            except Exception:
                ts = None
        if ts is not None:
            age = max(0.0, (now - ts).total_seconds() / 60.0)
            result["age_minutes"] = round(age, 2)
            result["is_stale"] = age > 3.0
        else:
            result["is_stale"] = False
        close = df["close"].astype(float)
        last = float(close.iloc[-1])
        result["last_price"] = last
        for minutes in (1, 3, 5, 15):
            if len(close) > minutes and float(close.iloc[-1 - minutes]) > 0:
                result["returns"][f"{minutes}m"] = round((last / float(close.iloc[-1 - minutes]) - 1.0) * 100.0, 4)
            else:
                result["returns"][f"{minutes}m"] = None
        if {"high", "low", "volume"}.issubset(df.columns) and df["volume"].fillna(0).sum() > 0:
            typical = (df["high"].astype(float) + df["low"].astype(float) + close) / 3.0
            vwap = float((typical * df["volume"].fillna(0).astype(float)).sum() / df["volume"].fillna(0).astype(float).sum())
        else:
            vwap = float(close.tail(min(20, len(close))).mean())
        result["vwap"] = round(vwap, 4)
        result["above_vwap"] = last >= vwap
        ema = close.ewm(span=min(5, len(close)), adjust=False).mean()
        if len(ema) >= 2 and float(ema.iloc[-2]) > 0:
            result["ema_slope_pct"] = round((float(ema.iloc[-1]) / float(ema.iloc[-2]) - 1.0) * 100.0, 4)
        highs = pd.to_numeric(df.get("high", close), errors="coerce").dropna().tail(4)
        lows = pd.to_numeric(df.get("low", close), errors="coerce").dropna().tail(4)
        if len(highs) >= 3:
            result["higher_highs"] = bool(highs.iloc[-1] > highs.iloc[-2] >= highs.iloc[-3])
            result["lower_highs"] = bool(highs.iloc[-1] < highs.iloc[-2] <= highs.iloc[-3])
        if len(lows) >= 3:
            result["higher_lows"] = bool(lows.iloc[-1] >= lows.iloc[-2] > lows.iloc[-3])
            result["lower_lows"] = bool(lows.iloc[-1] <= lows.iloc[-2] < lows.iloc[-3])
        r3 = result["returns"].get("3m")
        r5 = result["returns"].get("5m")
        ema_up = (result["ema_slope_pct"] or 0.0) > 0
        ema_down = (result["ema_slope_pct"] or 0.0) < 0
        result["hynix_uptrend_confirmed"] = bool(
            not result["is_stale"] and result["above_vwap"] and (r3 or 0) > 0 and (r5 or 0) > 0
            and result["higher_highs"] and result["higher_lows"] and ema_up
        )
        result["hynix_downtrend_confirmed"] = bool(
            not result["is_stale"] and not result["above_vwap"] and (r3 or 0) < 0 and (r5 or 0) < 0
            and result["lower_highs"] and result["lower_lows"] and ema_down
        )
        if result["hynix_uptrend_confirmed"]:
            result["direction"] = ACTION_HYNIX
            result["top_factors"] = ["above VWAP", "3m/5m returns positive", "higher highs/lows", "EMA slope up"]
        elif result["hynix_downtrend_confirmed"]:
            result["direction"] = ACTION_INVERSE
            result["top_factors"] = ["below VWAP", "3m/5m returns negative", "lower highs/lows", "EMA slope down"]
        else:
            result["top_factors"] = ["live trend not fully confirmed"]
        result["available"] = True
        return result
    except Exception as exc:
        result["top_factors"].append(f"live trend calculation failed: {exc}")
        return result

# ── 섹션 2: 초기 모델 가중치 ──────────────────────────────────────────────────
_BASE_WEIGHTS = {
    MODEL_ACTIVE_FUSION: 0.40,
    MODEL_PREDICTION_V2: 0.25,
    MODEL_EARLY_PREDICTION: 0.15,
    MODEL_CYCLE_AI: 0.10,
    MODEL_MICRON_PROXY: 0.10,
}

# Prediction V2/Cycle AI는 상태별 가중치 표가 명시됨. ACTIVE_FUSION은 이 엔진에서
# 항상 LIVE_VALIDATED로 취급한다(이미 수익 중인 검증된 전략을 대체하지 않기 위함).
# Early Prediction/Micron Proxy는 표가 없으므로 일반 감쇠 규칙(SHADOW=50%, DEGRADED=0%)을 적용한다.
_PREDICTION_V2_STATUS_WEIGHTS = {
    MODEL_STATUS_LIVE_VALIDATED: 0.25, MODEL_STATUS_ADVISORY: 0.15,
    MODEL_STATUS_SHADOW: 0.10, MODEL_STATUS_DEGRADED: 0.0,
}
_CYCLE_AI_STATUS_WEIGHTS = {
    MODEL_STATUS_LIVE_VALIDATED: 0.15, MODEL_STATUS_ADVISORY: 0.10,
    MODEL_STATUS_SHADOW: 0.05, MODEL_STATUS_DEGRADED: 0.0,
}


def _weight_for(model_name: str, model_status: str) -> float:
    if model_name == MODEL_ACTIVE_FUSION:
        return _BASE_WEIGHTS[MODEL_ACTIVE_FUSION]
    if model_name == MODEL_PREDICTION_V2:
        return _PREDICTION_V2_STATUS_WEIGHTS.get(model_status, 0.0)
    if model_name == MODEL_CYCLE_AI:
        return _CYCLE_AI_STATUS_WEIGHTS.get(model_status, 0.0)
    # Early Prediction / Micron Proxy — 일반 감쇠 규칙
    base = _BASE_WEIGHTS.get(model_name, 0.0)
    if model_status == MODEL_STATUS_DEGRADED:
        return 0.0
    if model_status == MODEL_STATUS_SHADOW:
        return base * 0.5
    return base


def build_model_result(
    model_name: str, hynix_probability: Optional[float], inverse_probability: Optional[float],
    hold_probability: Optional[float], confidence: Optional[float],
    recommended_position_pct: float, data_quality: float, model_status: str,
    reasons: Optional[list] = None,
) -> Optional[dict]:
    """섹션 1 — 각 모델의 표준 출력 스키마. 필수 확률이 없으면(데이터 없음) None을
    반환해 호출부가 이 모델을 완전히 제외하도록 한다(중립값 50을 채우지 않는다)."""
    if hynix_probability is None or inverse_probability is None:
        return None
    hold_probability = hold_probability if hold_probability is not None else max(
        0.0, 100.0 - hynix_probability - inverse_probability,
    )
    return {
        "model_name": model_name,
        "hynix_probability": round(float(hynix_probability), 2),
        "inverse_probability": round(float(inverse_probability), 2),
        "hold_probability": round(float(hold_probability), 2),
        "confidence": round(float(confidence), 2) if confidence is not None else 50.0,
        "recommended_position_pct": round(float(recommended_position_pct or 0.0), 2),
        "data_quality": round(float(data_quality), 2) if data_quality is not None else 50.0,
        "model_status": model_status,
        "reasons": list(reasons or []),
    }


def _implied_action(model_result: dict) -> str:
    h, i, o = model_result["hynix_probability"], model_result["inverse_probability"], model_result["hold_probability"]
    top = max(h, i, o)
    if top == o:
        return ACTION_HOLD
    return ACTION_HYNIX if h >= i else ACTION_INVERSE


def _map_active_strategy_action(action: Optional[str]) -> Optional[str]:
    """decide_active_strategy_action()의 action 문자열을 섹션 7 충돌처리가 쓰는
    ACTION_HYNIX/ACTION_INVERSE/ACTION_HOLD로 매핑한다. Scale-in/청산/전환류는
    "신규 진입 방향 충돌"과 무관하므로 HOLD로 취급한다(충돌규칙은 신규진입 판단용)."""
    if action == "ENTER_HYNIX":
        return ACTION_HYNIX
    if action == "ENTER_INVERSE":
        return ACTION_INVERSE
    return ACTION_HOLD


# =============================================================================
# 가중치 재정규화 (섹션 2) — 데이터 없는 모델은 중립값 50을 넣지 않고 제외한 뒤
# 남은 가중치를 100%로 재정규화한다.
# =============================================================================

def renormalize_weights(model_results: dict) -> dict:
    """model_results: {model_name: model_result_dict_or_None}.

    weight==0(DEGRADED)이거나 결과가 None인 모델은 제외한다. 남은 모델의 가중치
    합이 0이면(전부 제외) ACTIVE_FUSION만이라도 남아있으면 100%를 부여하고,
    그마저 없으면 빈 dict를 반환한다(→ 호출부는 HOLD로 처리해야 한다).
    """
    raw_weights = {}
    for name, result in model_results.items():
        if result is None:
            continue
        w = _weight_for(name, result.get("model_status", MODEL_STATUS_SHADOW))
        if w > 0:
            raw_weights[name] = w

    total = sum(raw_weights.values())
    if total <= 0:
        if model_results.get(MODEL_ACTIVE_FUSION) is not None:
            return {MODEL_ACTIVE_FUSION: 1.0}
        return {}
    return {name: w / total for name, w in raw_weights.items()}


# =============================================================================
# Fusion 결합
# =============================================================================

def _dampen_neutral_active_fusion(model_results: dict, weights: dict) -> dict:
    """ACTIVE_FUSION이 48~52(중립)일 때 이 모델의 실질 가중치를 낮춰, 다른 모델들의
    강한 신호(예: EARLY_PREDICTION 97%)가 ACTIVE_FUSION의 중립값에 완전히 상쇄되지
    않게 한다. ACTIVE_FUSION 자체가 방향성이 있을 때(중립 밴드 밖)는 그대로 40~50%
    가중치를 유지한다(이미 검증된 전략을 약화시키지 않기 위함)."""
    af = model_results.get(MODEL_ACTIVE_FUSION)
    if af is None or MODEL_ACTIVE_FUSION not in weights:
        return weights
    cfg = _load_fusion_v2_config()
    neutral_band = cfg["active_fusion_neutral_band"]
    neutral_scale = cfg["active_fusion_neutral_weight_scale"]
    if abs(af["hynix_probability"] - 50.0) > neutral_band:
        return weights
    adjusted = dict(weights)
    adjusted[MODEL_ACTIVE_FUSION] = adjusted[MODEL_ACTIVE_FUSION] * neutral_scale
    total = sum(adjusted.values())
    if total <= 0:
        return weights
    return {name: w / total for name, w in adjusted.items()}


def detect_strong_signal_conflict(model_results: dict) -> bool:
    """서로 다른 모델이 하이닉스/인버스 양방향 각각 강하게(>=70%) 신호를 내면 방향을
    확정하기 위험하므로, 가중합 결과와 무관하게 무조건 HOLD해야 한다(요구사항 2절
    단서 조항)."""
    threshold = _load_fusion_v2_config()["strong_signal_conflict_threshold"]
    strong_hynix = any(
        r is not None and r["hynix_probability"] >= threshold for r in model_results.values()
    )
    strong_inverse = any(
        r is not None and r["inverse_probability"] >= threshold for r in model_results.values()
    )
    return strong_hynix and strong_inverse


def evaluate_disagreement_override(model_results: dict) -> Optional[dict]:
    """가중합(fused_*)이 진입 문턱 미만이어도, 신뢰도 높은 단일 모델(confidence>=75)의
    방향에 최소 1개 다른 모델이 동조하고, 반대방향으로 "강하게"(>=65%) 신호를 내는
    모델이 2개 미만이면 탐색 진입(10~15%)을 허용한다(요구사항 2절).

    단순 가중평균만으로는 EARLY_PREDICTION처럼 확신도 높은 모델이 낮은 가중치 때문에
    묻히는 문제를 이 함수로 보완한다. detect_strong_signal_conflict()가 True이면
    호출부가 이 함수보다 우선해 HOLD로 덮어써야 한다.
    """
    cfg = _load_fusion_v2_config()
    leader_min_confidence = cfg["disagreement_leader_min_confidence"]
    max_opposing_strong = cfg["disagreement_max_opposing_strong"]
    opposing_strong_threshold = cfg["disagreement_opposing_strong_threshold"]
    entry_pct = cfg["disagreement_entry_pct"]

    leaders = []
    for name, r in model_results.items():
        if r is None:
            continue
        if r["confidence"] >= leader_min_confidence:
            action = _implied_action(r)
            if action != ACTION_HOLD:
                leaders.append((name, action, r["confidence"]))
    if not leaders:
        return None

    leader_name, leader_action, leader_confidence = max(leaders, key=lambda x: x[2])

    allies = [
        name for name, r in model_results.items()
        if r is not None and name != leader_name and _implied_action(r) == leader_action
    ]
    if not allies:
        return None

    opposing_strong = 0
    for name, r in model_results.items():
        if r is None or name == leader_name:
            continue
        opp_action = _implied_action(r)
        if opp_action == ACTION_HOLD or opp_action == leader_action:
            continue
        opp_prob = r["inverse_probability"] if leader_action == ACTION_HYNIX else r["hynix_probability"]
        if opp_prob >= opposing_strong_threshold:
            opposing_strong += 1

    if opposing_strong >= max_opposing_strong:
        return None

    return {
        "action": leader_action,
        "target_symbol": "000660" if leader_action == ACTION_HYNIX else "0197X0",
        "position_pct": entry_pct,
        "leader_model": leader_name, "leader_confidence": leader_confidence,
        "allies": allies, "opposing_strong_count": opposing_strong,
        "reason": (
            f"모델 불일치 예외 진입: {leader_name}(conf={leader_confidence:.0f}%) 방향 + "
            f"동조 {len(allies)}개, 강반대모델 {opposing_strong}개(<{max_opposing_strong})"
        ),
    }


def fuse_model_results(model_results: dict) -> dict:
    """섹션 1 — 최종 fused_* 값 계산. model_agreement는 dominant 방향에 동의하는
    모델의 가중치 비중(%)이다(단순 개수가 아니라 가중치 기준 — 가중치가 큰 모델의
    동의가 더 크게 반영되어야 하기 때문)."""
    weights = renormalize_weights(model_results)
    weights = _dampen_neutral_active_fusion(model_results, weights)
    if not weights:
        return {
            "fused_hynix_probability": 0.0, "fused_inverse_probability": 0.0,
            "fused_hold_probability": 100.0, "fused_confidence": 0.0,
            "dominant_model": None, "model_agreement": 0.0, "weights": {},
            "final_action": ACTION_HOLD, "reasons": ["사용 가능한 모델 없음 — HOLD"],
        }

    fused_hynix = fused_inverse = fused_hold = fused_conf = 0.0
    for name, w in weights.items():
        r = model_results[name]
        fused_hynix += w * r["hynix_probability"]
        fused_inverse += w * r["inverse_probability"]
        fused_hold += w * r["hold_probability"]
        fused_conf += w * r["confidence"]

    dominant_model = max(weights.items(), key=lambda kv: kv[1])[0]

    if fused_hold >= fused_hynix and fused_hold >= fused_inverse:
        final_action = ACTION_HOLD
    elif fused_hynix >= fused_inverse:
        final_action = ACTION_HYNIX
    else:
        final_action = ACTION_INVERSE

    agree_weight = 0.0
    for name, w in weights.items():
        if _implied_action(model_results[name]) == final_action:
            agree_weight += w
    model_agreement = round(agree_weight * 100.0, 1)

    reasons = [
        f"{name}(w={w*100:.0f}%,status={model_results[name]['model_status']}): "
        f"H{model_results[name]['hynix_probability']:.0f}/I{model_results[name]['inverse_probability']:.0f}"
        f"/Hold{model_results[name]['hold_probability']:.0f}"
        for name, w in sorted(weights.items(), key=lambda kv: -kv[1])
    ]

    return {
        "fused_hynix_probability": round(fused_hynix, 2), "fused_inverse_probability": round(fused_inverse, 2),
        "fused_hold_probability": round(fused_hold, 2), "fused_confidence": round(fused_conf, 2),
        "dominant_model": dominant_model, "model_agreement": model_agreement, "weights": weights,
        "final_action": final_action, "reasons": reasons,
    }


# =============================================================================
# 섹션 7 — ACTIVE와 Prediction V2 충돌 처리
# =============================================================================

def apply_conflict_resolution(
    fused: dict, active_action: Optional[str], prediction_v2_action: Optional[str],
    cycle_phase: Optional[str], base_position_pct: float,
) -> dict:
    """ACTIVE_FUSION과 Prediction V2의 독자 판단이 같은/반대/HOLD일 때의 조정(Case A~C),
    Cycle AI NO_TRADE/GAP_FAILURE/TREND_DOWN 조정(Case D~E)을 적용한다.

    반환: {"position_pct": float, "confidence_adjust": float, "force_hold": bool, "notes": [...]}
    """
    position_pct = base_position_pct
    confidence_adjust = 0.0
    force_hold = False
    notes: list[str] = []

    if active_action and prediction_v2_action:
        if active_action == ACTION_HOLD:
            pass  # ACTIVE 자체가 HOLD면 충돌 규칙 대상 아님(그냥 HOLD)
        elif prediction_v2_action == ACTION_HOLD:
            # Case A: ACTIVE=방향, Prediction V2=HOLD → 거래는 허용하되 비중 25% 축소
            position_pct *= 0.75
            notes.append("Case A: Prediction V2 HOLD — 비중 25% 축소, 거래는 허용")
        elif active_action == prediction_v2_action:
            # Case C: 둘 다 같은 방향 → 확대(강한 합의)
            position_pct *= 1.15
            notes.append("Case C: ACTIVE/Prediction V2 동일 방향 — 비중 확대")
        else:
            # Case B: 서로 반대 방향
            diff = abs(fused["fused_hynix_probability"] - fused["fused_inverse_probability"])
            if diff < 10.0:
                force_hold = True
                notes.append(f"Case B: ACTIVE/Prediction V2 반대방향, 확률차 {diff:.1f}%p<10 — HOLD")
            elif diff < 20.0:
                position_pct = min(position_pct, 10.0)
                notes.append(f"Case B: 확률차 {diff:.1f}%p(10~20) — 10% 시험진입만")
            else:
                position_pct = max(20.0, min(position_pct, 30.0))
                notes.append(f"Case B: 확률차 {diff:.1f}%p(>=20) — 우세방향 20~30% 진입")

    if cycle_phase == "NO_TRADE":
        # Case D: 단독 차단 아님 — confidence -8, 비중 30% 축소
        confidence_adjust -= 8.0
        position_pct *= 0.70
        notes.append("Case D: Cycle AI NO_TRADE — 단독차단 없음, confidence-8/비중 30% 축소")
    elif cycle_phase in ("GAP_FAILURE", "TREND_DOWN", "BREAKDOWN"):
        # Case E: 인버스 방향 가점
        if fused["final_action"] == ACTION_INVERSE:
            position_pct *= 1.10
            notes.append(f"Case E: Cycle Phase {cycle_phase} — 인버스 방향 가점(+10%)")

    return {
        "position_pct": round(max(0.0, position_pct), 2),
        "confidence_adjust": confidence_adjust, "force_hold": force_hold, "notes": notes,
    }


# =============================================================================
# 섹션 4 — 진입 사다리(우세 방향 확률 → 비중)
#
# 2026-07-14 개편: 단일 55% 게이트 때문에 모델 의견이 조금만 엇갈려도(예: 45%대)
# 하루 종일 거래가 0건이 되는 문제가 실측됐다(가중합 55%를 하루 종일 못 넘김).
# 52%부터 4단계로 세분화해 저확신 신호는 소액(탐색), 고확신 신호는 확대(정상)
# 진입하도록 바꾼다 — "억지 전액매매"가 아니라 "확신도에 비례한 사이징"이다.
# =============================================================================

_ENTRY_LADDER = [
    (68.0, 65.0), (60.0, 45.0), (55.0, 25.0), (52.0, 10.0),
]
_ENTRY_MIN_PROBABILITY = 52.0
_ENTRY_MIN_CONFIDENCE = 52.0
_ENTRY_MAX_OPPOSITE_PROBABILITY = 42.0
_ENTRY_MIN_EXPECTED_MOVE_5M_PCT = 0.15
_ENTRY_EXPLORATORY_MAX_PCT = 10.0  # 이 이하 비중은 "탐색 진입"으로 태깅(손절폭이 더 타이트함)


def _entry_ladder_from_config(cfg: dict) -> list:
    """config의 entry_ladder([{"floor":..,"pct":..}, ...])를 (floor, pct) 튜플 목록으로
    변환하고 floor 내림차순으로 정렬한다(설정 파일의 순서에 의존하지 않기 위함)."""
    rows = cfg.get("entry_ladder") or _FUSION_V2_DEFAULTS["entry_ladder"]
    pairs = [(float(r["floor"]), float(r["pct"])) for r in rows]
    pairs.sort(key=lambda p: -p[0])
    return pairs


def position_pct_from_probability_ladder(dominant_probability: float, tier_reduction: int = 0, floor_relief: float = 0.0) -> float:
    """dominant_probability(우세 방향 확률)를 4단계 사다리(52/55/60/68%, config로 조정
    가능)로 비중(%)에 매핑한다. tier_reduction>0이면 시장급변/데이터stale/리스크경고
    시 한 단계씩 낮은 비중으로 축소한다(0으로 완전히 죽이지는 않음 — 신호 자체는
    유효하기 때문). floor_relief(>=0)는 HOLD 완화/시간대별 문턱완화(threshold relief)를
    사다리 바닥에도 동일하게 반영한다 — 그렇지 않으면 문턱완화가 entry_threshold_used
    표시에만 나타나고 실제 진입비중 산정(사다리 최저 52% 고정 바닥)에는 전혀 영향을
    주지 못해, "완화"가 이름뿐인 채로 계속 진입 0건이 되는 버그가 있었다(2026-07-14
    실측: fused_inverse_probability 49.73%, threshold_used 50.5%였는데도 target_position_pct
    0%로 매 사이클 차단됨)."""
    ladder = _entry_ladder_from_config(_load_fusion_v2_config())
    idx = None
    for i, (floor, _pct) in enumerate(ladder):
        if dominant_probability >= floor - floor_relief:
            idx = i
            break
    if idx is None:
        return 0.0
    idx = min(idx + max(0, tier_reduction), len(ladder) - 1)
    return ladder[idx][1]


def entry_gate_ok(dominant_probability: float, opposite_probability: float, confidence: float, expected_move_5m_pct: Optional[float]) -> Optional[str]:
    """진입 게이트 통과 여부. 통과하면 None, 막히면 이유 문자열."""
    cfg = _load_fusion_v2_config()
    min_probability = cfg["entry_min_probability"]
    min_confidence = cfg["entry_min_probability"]  # 확신도 최소치도 진입 문턱과 동일하게 관리
    if dominant_probability < min_probability:
        return f"우세방향 확률 {dominant_probability:.1f}% < {min_probability}%"
    if confidence < min_confidence:
        return f"confidence {confidence:.1f} < {min_confidence}"
    if opposite_probability > _ENTRY_MAX_OPPOSITE_PROBABILITY:
        return f"반대확률 {opposite_probability:.1f}% > {_ENTRY_MAX_OPPOSITE_PROBABILITY}%"
    if expected_move_5m_pct is not None and expected_move_5m_pct < _ENTRY_MIN_EXPECTED_MOVE_5M_PCT:
        return f"expected_move_5m {expected_move_5m_pct:.2f}% < {_ENTRY_MIN_EXPECTED_MOVE_5M_PCT}%"
    return None


# =============================================================================
# 섹션 5 — HOLD 과다 방지(Threshold 완화)
# =============================================================================

def default_hold_tracker() -> dict:
    return {"cycle_history": [], "exploratory_entry_used_today": False, "_state_date": None}


def update_hold_tracker(tracker: Optional[dict], has_position: bool, action: str, now: datetime) -> dict:
    tracker = dict(tracker) if tracker else default_hold_tracker()
    today = now.strftime("%Y%m%d")
    if tracker.get("_state_date") != today:
        tracker = default_hold_tracker()
        tracker["_state_date"] = today

    history = list(tracker.get("cycle_history", []))
    history.append({"at": now.isoformat(), "action": action, "has_position": has_position})
    cutoff = now - timedelta(minutes=20)
    pruned = []
    for h in history:
        try:
            if datetime.fromisoformat(h["at"]) >= cutoff:
                pruned.append(h)
        except Exception:
            continue
    tracker["cycle_history"] = pruned[-200:]
    return tracker


def compute_threshold_relief(
    tracker: dict, cycle_phase: Optional[str], confidence: float, opposite_probability: float,
    expected_move_5m_pct: Optional[float], data_quality: float, consecutive_stop_losses: int,
) -> dict:
    """최근 20분 무포지션+유효사이클 4회 이상+HOLD 4회 이상이면 -2점, 7회 이상이면 -4점.

    단, 아래면 완화 금지: NO_TRADE이면서 confidence<55, expected_move_5m<0.15%,
    반대확률>45%, data_quality<60, 최근 2회 연속 손절.
    """
    history = tracker.get("cycle_history", [])
    no_position_history = [h for h in history if not h.get("has_position")]
    valid_cycles = len(no_position_history)
    hold_count = sum(1 for h in no_position_history if h.get("action") == ACTION_HOLD)

    blocked_reasons = []
    if cycle_phase == "NO_TRADE" and confidence < 55.0:
        blocked_reasons.append("NO_TRADE이면서 confidence<55")
    if expected_move_5m_pct is not None and expected_move_5m_pct < 0.15:
        blocked_reasons.append("expected_move_5m<0.15%")
    if opposite_probability > 45.0:
        blocked_reasons.append("반대방향 확률>45%")
    if data_quality < 60.0:
        blocked_reasons.append("data_quality<60")
    if consecutive_stop_losses >= 2:
        blocked_reasons.append("최근 2회 연속 손절")

    relief = 0.0
    if not blocked_reasons and valid_cycles >= 4 and hold_count >= 4:
        relief = 2.0
        if hold_count >= 7:
            relief = 4.0

    return {
        "relief": relief, "valid_cycles": valid_cycles, "hold_count": hold_count,
        "relief_blocked": bool(blocked_reasons), "blocked_reasons": blocked_reasons,
    }


TRIAL_ENTRY_MIN_THRESHOLD = 50.0
GENERAL_ENTRY_MIN_THRESHOLD = 60.0

_TIME_RELIEF_STAGE_2_MAX_ORDERS = 1


def time_based_threshold_relief(now: datetime, orders_today_count: int, daily_return_pct: Optional[float]) -> dict:
    """목표 거래횟수(하루 4~5회) 확보를 위한 시간대별 문턱 완화(요구사항 3절, config로
    조정 가능).

    이 함수는 문턱을 낮출 뿐 강제로 진입을 발생시키지 않는다 — 신호 자체가 없으면
    완화돼도 여전히 거래되지 않는다. 오후(13:00 이후) 일손익이 음수면 과매매 방지를
    위해 완화를 적용하지 않는다.
    """
    cfg = _load_fusion_v2_config()
    stage_1_time = cfg["time_relief_stage_1_time"]
    stage_1_pct = cfg["time_relief_stage_1_pct"]
    stage_2_time = cfg["time_relief_stage_2_time"]
    stage_2_pct = cfg["time_relief_stage_2_pct"]
    max_total_pct = cfg["time_relief_max_total_pct"]

    now_str = now.strftime("%H:%M")
    if now_str >= stage_2_time and daily_return_pct is not None and daily_return_pct < 0:
        return {"relief": 0.0, "reasons": [f"오후({stage_2_time} 이후) 일손익 음수 — 문턱 완화 적용 안 함"]}

    relief = 0.0
    reasons: list[str] = []
    if now_str >= stage_1_time and orders_today_count == 0:
        relief += stage_1_pct
        reasons.append(f"{stage_1_time}까지 거래 0건 — 문턱 {stage_1_pct}%p 완화")
    if now_str >= stage_2_time and orders_today_count <= _TIME_RELIEF_STAGE_2_MAX_ORDERS:
        relief += stage_2_pct
        reasons.append(f"{stage_2_time}까지 거래 {_TIME_RELIEF_STAGE_2_MAX_ORDERS}건 이하 — 문턱 {stage_2_pct}%p 추가 완화")
    relief = min(relief, max_total_pct)
    return {"relief": relief, "reasons": reasons}


def should_allow_exploratory_entry(
    tracker: dict, now: datetime, orders_today_count: int, dominant_probability: float,
    confidence: float, expected_move_5m_pct: Optional[float], expected_value: Optional[float],
) -> Optional[str]:
    """13:30까지 운영 주문 0건이면 조건 충족 시 10% 탐색 진입 1회 허용. 허용되면 사유
    문자열, 아니면 None."""
    if tracker.get("exploratory_entry_used_today"):
        return None
    if now.strftime("%H:%M") > "13:30":
        return None
    if orders_today_count != 0:
        return None
    if dominant_probability < 55.0 or confidence < 55.0:
        return None
    if expected_move_5m_pct is None or expected_move_5m_pct < 0.18:
        return None
    if expected_value is None or expected_value <= 0:
        return None
    return "13:30까지 운영 주문 0건 — 10% 탐색 진입 1회 허용"


# =============================================================================
# 섹션 10 — Expected Value 기반 포지션 사이징
# =============================================================================

_EV_LADDER = [(0.60, 85.0), (0.35, 50.0), (0.20, 35.0), (0.10, 20.0), (0.0, 10.0)]


def calculate_expected_value(
    win_probability_pct: float, expected_profit_pct: float, expected_loss_pct: float,
    estimated_fees_pct: float = 0.015, estimated_slippage_pct: float = 0.02,
) -> float:
    win_p = max(0.0, min(100.0, win_probability_pct)) / 100.0
    loss_p = 1.0 - win_p
    ev = (
        win_p * expected_profit_pct - loss_p * expected_loss_pct
        - estimated_fees_pct - estimated_slippage_pct
    )
    return round(ev, 4)


def position_pct_from_expected_value(expected_value: float) -> float:
    if expected_value <= 0:
        return 0.0
    for floor, pct in _EV_LADDER:
        if expected_value >= floor:
            return pct
    return 0.0


def calculate_final_position_pct(probability_ladder_pct: float, expected_value: float) -> dict:
    """확률기준과 EV기준 중 더 낮은 비중을 적용한다."""
    ev_pct = position_pct_from_expected_value(expected_value)
    final_pct = min(probability_ladder_pct, ev_pct)
    return {"final_pct": round(final_pct, 2), "probability_ladder_pct": probability_ladder_pct, "ev_ladder_pct": ev_pct}


# =============================================================================
# 섹션 6 — 조기 진입(Early Entry)
# =============================================================================

def evaluate_early_entry_hynix(
    momentum_inflection_up: Optional[float], up_probability_3m: Optional[float], up_probability_5m: Optional[float],
    down_probability_3m: Optional[float], recent_low_not_renewed: bool, acceleration_improving: bool,
    expected_move_5m_pct: Optional[float],
) -> Optional[dict]:
    if None in (momentum_inflection_up, up_probability_3m, up_probability_5m, down_probability_3m, expected_move_5m_pct):
        return None
    ok = (
        momentum_inflection_up >= 62.0 and up_probability_3m >= 58.0 and up_probability_5m >= 60.0
        and down_probability_3m <= 40.0 and recent_low_not_renewed and acceleration_improving
        and expected_move_5m_pct >= 0.18
    )
    if not ok:
        return None
    return {"symbol": "000660", "position_pct": 15.0, "reason": "조기진입(하이닉스): momentum/turning point 조건 충족"}


def evaluate_early_entry_inverse(
    momentum_inflection_down: Optional[float], down_probability_3m: Optional[float], down_probability_5m: Optional[float],
    up_probability_3m: Optional[float], recent_high_not_renewed_or_vwap_broken: bool,
    expected_move_5m_pct: Optional[float],
) -> Optional[dict]:
    if None in (momentum_inflection_down, down_probability_3m, down_probability_5m, up_probability_3m, expected_move_5m_pct):
        return None
    ok = (
        momentum_inflection_down >= 62.0 and down_probability_3m >= 58.0 and down_probability_5m >= 60.0
        and up_probability_3m <= 40.0 and recent_high_not_renewed_or_vwap_broken
        and expected_move_5m_pct >= 0.18
    )
    if not ok:
        return None
    return {"symbol": "0197X0", "position_pct": 15.0, "reason": "조기진입(인버스): momentum/turning point 조건 충족"}


def default_early_entry_state() -> dict:
    return {"active": False, "symbol": None, "entered_at": None, "last_reeval_at": None, "current_pct": 0.0}


def reevaluate_early_entry(early_state: dict, now: datetime, current_probability: float) -> dict:
    """90초마다 재평가: 68%->35%, 75%->55%, 82%->70%, 55% 아래->전량 시험청산."""
    state = dict(early_state)
    last = state.get("last_reeval_at")
    if last:
        try:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        except Exception:
            elapsed = 999.0
        if elapsed < 90:
            return {"changed": False, "state": state, "target_pct": state.get("current_pct", 0.0)}

    state["last_reeval_at"] = now.isoformat()
    if current_probability < 55.0:
        state["active"] = False
        state["current_pct"] = 0.0
        return {"changed": True, "state": state, "target_pct": 0.0, "reason": f"확률 {current_probability:.0f}%<55 — 전량 시험청산"}
    if current_probability >= 82.0:
        target = 70.0
    elif current_probability >= 75.0:
        target = 55.0
    elif current_probability >= 68.0:
        target = 35.0
    else:
        target = state.get("current_pct", 15.0)
    state["current_pct"] = target
    return {"changed": target != early_state.get("current_pct"), "state": state, "target_pct": target}


# =============================================================================
# 섹션 8 — 빠른 선제청산
# =============================================================================

def evaluate_preemptive_exit(
    held_symbol: str, inverse_probability: float, hynix_probability: float,
    down_turn_probability_3m: Optional[float], up_turn_probability_3m: Optional[float],
    momentum_inflection_down: Optional[float], momentum_inflection_up: Optional[float],
    exit_long_probability: Optional[float] = None, exit_short_probability: Optional[float] = None,
    current_profit_pct: Optional[float] = None,
) -> Optional[dict]:
    """하이닉스/인버스 보유 중 예측 확률 악화 시 손절선 전에 선제적으로 축소한다.
    반환 None이면 조치 없음, dict면 {"ratio": 매도비중, "reason": str}."""
    is_hynix = held_symbol == "000660"
    opposite_prob = inverse_probability if is_hynix else hynix_probability
    opposite_turn_3m = down_turn_probability_3m if is_hynix else up_turn_probability_3m
    opposite_inflection = momentum_inflection_down if is_hynix else momentum_inflection_up
    exit_prob = (exit_long_probability if is_hynix else exit_short_probability) or opposite_prob

    if current_profit_pct is not None:
        if current_profit_pct >= 1.2 and opposite_prob >= 55.0:
            return {"ratio": 0.0, "profit_lock": True, "reason": f"수익 {current_profit_pct:+.2f}% — Profit Lock 활성화"}
        if current_profit_pct >= 0.8 and opposite_prob >= 58.0:
            return {"ratio": 0.5, "reason": f"수익 {current_profit_pct:+.2f}%, 반대확률 {opposite_prob:.0f}% — 50% 수익보호"}
        if current_profit_pct >= 0.4 and opposite_prob >= 60.0:
            return {"ratio": 0.25, "reason": f"수익 {current_profit_pct:+.2f}%, 반대확률 {opposite_prob:.0f}% — 25% 수익보호"}

    if exit_prob >= 76.0:
        return {"ratio": 1.0, "reason": f"exit_probability {exit_prob:.0f}%>=76 — 전량청산"}
    if exit_prob >= 68.0:
        return {"ratio": 0.5, "reason": f"exit_probability {exit_prob:.0f}%>=68 — 50% 청산"}
    if (
        opposite_prob >= 58.0 and (opposite_turn_3m or 0) >= 60.0 and (opposite_inflection or 0) >= 60.0
    ):
        return {"ratio": 0.25, "reason": f"반대확률 {opposite_prob:.0f}%+전환확률+모멘텀 모두 충족 — 25% 선제청산"}
    return None


# =============================================================================
# 섹션 9 — 재진입 및 휩쏘 방지
# =============================================================================

_REENTRY_AFTER_TP_SECONDS = 180
_REENTRY_AFTER_TP_FAST_SECONDS = 90
_REENTRY_AFTER_TP_FAST_PROBABILITY = 80.0
_REENTRY_AFTER_SL_SECONDS = 600
_REENTRY_AFTER_SL_EXCEPTION_SECONDS = 180
_REENTRY_AFTER_SL_EXCEPTION_PROBABILITY = 85.0
_REENTRY_AFTER_SL_EXCEPTION_CONFIDENCE = 80.0
_WHIPSAW_WINDOW_SECONDS = 600
_WHIPSAW_FLIP_LIMIT = 2
_WHIPSAW_DAMPEN_SECONDS = 900
_WHIPSAW_THRESHOLD_PENALTY = 5.0
_WHIPSAW_POSITION_SCALE = 0.5


def check_reentry_cooldown(
    last_exit_time: Optional[str], was_take_profit: bool, now: datetime,
    dominant_probability: float, confidence: float, trend_rebreak_confirmed: bool = False,
) -> Optional[str]:
    """재진입이 아직 금지 상태이면 사유 문자열, 허용되면 None."""
    if not last_exit_time:
        return None
    try:
        exit_dt = datetime.fromisoformat(last_exit_time)
    except Exception:
        return None
    elapsed = (now - exit_dt).total_seconds()

    if was_take_profit:
        cooldown = _REENTRY_AFTER_TP_FAST_SECONDS if dominant_probability >= _REENTRY_AFTER_TP_FAST_PROBABILITY else _REENTRY_AFTER_TP_SECONDS
        if elapsed < cooldown:
            return f"익절 후 재진입 대기 중({cooldown}s, {int(elapsed)}s 경과)"
        return None

    # 손절 후: 기본 10분, 예외 조건(확률85+conf80+추세재돌파) 충족 시 3분
    if (
        dominant_probability >= _REENTRY_AFTER_SL_EXCEPTION_PROBABILITY
        and confidence >= _REENTRY_AFTER_SL_EXCEPTION_CONFIDENCE and trend_rebreak_confirmed
    ):
        if elapsed < _REENTRY_AFTER_SL_EXCEPTION_SECONDS:
            return f"손절 후 예외 재진입 대기 중({_REENTRY_AFTER_SL_EXCEPTION_SECONDS}s, {int(elapsed)}s 경과)"
        return None
    if elapsed < _REENTRY_AFTER_SL_SECONDS:
        return f"손절 후 재진입 대기 중({_REENTRY_AFTER_SL_SECONDS}s, {int(elapsed)}s 경과)"
    return None


# =============================================================================
# 섹션 17 — 거래 빈도 관리(목표 4~5회/일, 왕복 6회 제한, 동일방향 재진입 쿨다운)
# =============================================================================

DAILY_TARGET_TRADES_MIN = 4
DAILY_TARGET_TRADES_MAX = 5


def default_frequency_state() -> dict:
    return {
        "_state_date": None, "round_trips_today": 0,
        "last_entry_direction": None, "last_entry_time": None,
    }


def _reset_frequency_state_if_new_day(state: dict, now: datetime) -> dict:
    state = dict(state) if state else default_frequency_state()
    today = now.strftime("%Y%m%d")
    if state.get("_state_date") != today:
        state = default_frequency_state()
        state["_state_date"] = today
    return state


def check_frequency_limits(frequency_state: dict, desired_action: str, now: datetime) -> Optional[str]:
    """일 최대 왕복거래(기본 6회)/동일방향 재진입 쿨다운(기본 10분, config로 조정
    가능) 위반 시 차단 사유를 반환한다(통과하면 None). 강제 주문으로 최소 거래횟수를
    채우지 않는다 — 이 함수는 "너무 많이" 거래하는 것만 막을 뿐, 거래를 발생시키지
    않는다."""
    cfg = _load_fusion_v2_config()
    max_round_trips = cfg["max_daily_round_trips"]
    cooldown_seconds = cfg["same_direction_reentry_cooldown_seconds"]

    state = _reset_frequency_state_if_new_day(frequency_state, now)
    if state.get("round_trips_today", 0) >= max_round_trips:
        return f"일 최대 왕복거래 {max_round_trips}회 도달 — 신규진입 중단"

    last_dir = state.get("last_entry_direction")
    last_time = state.get("last_entry_time")
    if last_dir == desired_action and last_time:
        try:
            elapsed = (now - datetime.fromisoformat(last_time)).total_seconds()
        except Exception:
            elapsed = cooldown_seconds
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            return f"동일방향({desired_action}) 재진입 쿨다운 중(남은 {remaining}s)"
    return None


def register_frequency_entry(frequency_state: dict, action: str, now: datetime) -> dict:
    state = _reset_frequency_state_if_new_day(frequency_state, now)
    state["last_entry_direction"] = action
    state["last_entry_time"] = now.isoformat()
    return state


def register_frequency_round_trip_closed(frequency_state: dict, now: datetime) -> dict:
    state = _reset_frequency_state_if_new_day(frequency_state, now)
    state["round_trips_today"] = state.get("round_trips_today", 0) + 1
    return state


def default_whipsaw_state() -> dict:
    return {"flip_history": [], "dampened_until": None}


def register_direction_flip(whipsaw_state: dict, now: datetime) -> dict:
    state = dict(whipsaw_state) if whipsaw_state else default_whipsaw_state()
    history = [h for h in state.get("flip_history", []) if _within(h, now, _WHIPSAW_WINDOW_SECONDS)]
    history.append(now.isoformat())
    state["flip_history"] = history
    if len(history) >= _WHIPSAW_FLIP_LIMIT + 1:
        state["dampened_until"] = (now + timedelta(seconds=_WHIPSAW_DAMPEN_SECONDS)).isoformat()
    return state


def _within(iso_ts: str, now: datetime, seconds: float) -> bool:
    try:
        return (now - datetime.fromisoformat(iso_ts)).total_seconds() <= seconds
    except Exception:
        return False


def is_whipsaw_dampened(whipsaw_state: dict, now: datetime) -> bool:
    until = whipsaw_state.get("dampened_until")
    if not until:
        return False
    try:
        return now < datetime.fromisoformat(until)
    except Exception:
        return False


def apply_whipsaw_dampening(position_pct: float, threshold: float, whipsaw_state: dict, now: datetime) -> dict:
    if is_whipsaw_dampened(whipsaw_state, now):
        return {
            "position_pct": round(position_pct * _WHIPSAW_POSITION_SCALE, 2),
            "threshold": threshold + _WHIPSAW_THRESHOLD_PENALTY,
            "dampened": True,
        }
    return {"position_pct": position_pct, "threshold": threshold, "dampened": False}


# =============================================================================
# 섹션 16 — 일 손익 기반 리스크 사다리(Adaptive Fusion 전용 — hynix_trading_mode의
# 것과 숫자가 다르므로 별도 함수로 둔다)
# =============================================================================

def adaptive_fusion_daily_risk_ladder(daily_return_pct: Optional[float]) -> dict:
    if daily_return_pct is None:
        return {"max_position_pct": 100.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}

    if daily_return_pct <= -2.5:
        return {"max_position_pct": 0.0, "threshold_add": 0.0, "entries_allowed": False, "force_liquidate": True}
    if daily_return_pct <= -2.0:
        # 요구사항 4절 — 일 손실 -2% 도달 시 신규진입 중단(전량청산까지는 -2.5%에서).
        return {"max_position_pct": 0.0, "threshold_add": 0.0, "entries_allowed": False, "force_liquidate": False}
    if daily_return_pct <= -1.2:
        return {"max_position_pct": 40.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct <= -0.8:
        return {"max_position_pct": 70.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct >= 3.0:
        # 요구사항 4절 — 일 수익 +3% 이후 "수익보호 모드": 완전 차단이 아니라 비중을
        # 크게 축소하고(최대 20%) 문턱을 높여(+6) 아주 확신 높은 신호만 소액 추가한다.
        return {"max_position_pct": 20.0, "threshold_add": 6.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct >= 2.0:
        return {"max_position_pct": 50.0, "threshold_add": 3.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct >= 1.0:
        return {"max_position_pct": 70.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    return {"max_position_pct": 100.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}


# =============================================================================
# 섹션 11 — Prediction AI V2 성능 기반 자동 감쇠
# =============================================================================

_PV2_LOG_PATH = ROOT / "data" / "logs" / "prediction_v2_snapshot_log.csv"
_PV2_PENDING_PATH = ROOT / "data" / "state" / "hynix_prediction_v2_pending.json"
_PV2_LOG_COLUMNS = [
    "decision_timestamp", "outcome_timestamp", "horizon_minutes", "predicted_action",
    "buy_probability", "sell_probability", "hynix_price_at_decision", "hynix_price_at_outcome",
    "inverse_price_at_decision", "inverse_price_at_outcome", "hynix_return_pct", "inverse_return_pct",
]

PV2_DEGRADE_PF_MIN = 0.9
PV2_DEGRADE_BASELINE_BRIER = 0.25
PV2_DEGRADE_MIN_VALID_FRACTION = 0.70
PV2_RECOVER_MIN_SAMPLES = 200
PV2_RECOVER_PF_MIN = 1.1


def record_prediction_v2_snapshot(
    now: datetime, predicted_action: str, buy_probability: float, sell_probability: float,
    hynix_price: Optional[float], inverse_price: Optional[float],
) -> None:
    if hynix_price is None:
        return
    pending = _load_pv2_pending()
    for horizon in (3, 5, 10):
        pending.append({
            "decision_timestamp": now.isoformat(), "horizon_minutes": horizon,
            "target_time": (now + timedelta(minutes=horizon)).isoformat(),
            "predicted_action": predicted_action, "buy_probability": buy_probability,
            "sell_probability": sell_probability,
            "hynix_price_at_decision": hynix_price, "inverse_price_at_decision": inverse_price,
        })
    _save_pv2_pending(pending)


def resolve_prediction_v2_outcomes(now: datetime, hynix_price: Optional[float], inverse_price: Optional[float]) -> list:
    if hynix_price is None:
        return []
    pending = _load_pv2_pending()
    if not pending:
        return []
    resolved, remaining = [], []
    for item in pending:
        try:
            target = datetime.fromisoformat(item["target_time"])
        except Exception:
            continue
        if now < target:
            remaining.append(item)
            continue
        dp = item.get("hynix_price_at_decision")
        dip = item.get("inverse_price_at_decision")
        hynix_ret = round((hynix_price / dp - 1.0) * 100, 4) if dp else None
        inverse_ret = round((inverse_price / dip - 1.0) * 100, 4) if (inverse_price is not None and dip) else None
        row = {
            "decision_timestamp": item["decision_timestamp"], "outcome_timestamp": now.isoformat(),
            "horizon_minutes": item["horizon_minutes"], "predicted_action": item.get("predicted_action"),
            "buy_probability": item.get("buy_probability"), "sell_probability": item.get("sell_probability"),
            "hynix_price_at_decision": dp, "hynix_price_at_outcome": hynix_price,
            "inverse_price_at_decision": dip, "inverse_price_at_outcome": inverse_price,
            "hynix_return_pct": hynix_ret, "inverse_return_pct": inverse_ret,
        }
        _append_pv2_csv(row)
        resolved.append(row)
    if len(remaining) != len(pending):
        _save_pv2_pending(remaining)
    return resolved


def _load_pv2_pending() -> list:
    try:
        if not _PV2_PENDING_PATH.exists():
            return []
        data = json.loads(_PV2_PENDING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pv2_pending(pending: list) -> None:
    try:
        _PV2_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PV2_PENDING_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pending, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, _PV2_PENDING_PATH)
    except Exception as exc:
        logger.debug("[AdaptiveFusion] Prediction V2 pending 저장 실패: %s", exc)


def _append_pv2_csv(row: dict) -> None:
    try:
        _PV2_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _PV2_LOG_PATH.exists()
        with _PV2_LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_PV2_LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({c: row.get(c, "") for c in _PV2_LOG_COLUMNS})
    except Exception as exc:
        logger.debug("[AdaptiveFusion] Prediction V2 로그 기록 실패: %s", exc)


def load_prediction_v2_outcome_log() -> pd.DataFrame:
    if not _PV2_LOG_PATH.exists():
        return pd.DataFrame(columns=_PV2_LOG_COLUMNS)
    try:
        df = pd.read_csv(_PV2_LOG_PATH)
    except Exception:
        return pd.DataFrame(columns=_PV2_LOG_COLUMNS)
    return df


def evaluate_prediction_v2_performance(now: Optional[datetime] = None, horizon_minutes: int = 5) -> dict:
    """최근 rolling 100건(horizon=5분) 기준 Profit Factor/Brier/Precision/평균수익/
    유효표본비율을 계산해 model_status를 결정한다. 회복 조건(최근 200건, PF>=1.1,
    최근 3거래일 평균수익 양수)을 만족하면 DEGRADED에서 ADVISORY로 복귀시킨다."""
    now = now or datetime.now()
    df = load_prediction_v2_outcome_log()
    empty_result = {
        "model_status": MODEL_STATUS_SHADOW, "sample_size": 0, "valid_sample_fraction": 0.0,
        "profit_factor": None, "avg_return_pct": None, "brier_score": None, "precision": None,
        "reason": "표본 없음 — SHADOW 유지",
    }
    if df.empty:
        return empty_result

    sub = df[df["horizon_minutes"].astype(str) == str(horizon_minutes)].copy()
    if sub.empty:
        return empty_result

    sub["decision_timestamp"] = pd.to_datetime(sub["decision_timestamp"], errors="coerce")
    sub = sub.dropna(subset=["decision_timestamp"]).sort_values("decision_timestamp")
    attempted = sub.tail(100)

    trades, hits, forecast_probs, outcomes = [], 0, [], []
    valid = 0
    for _, row in attempted.iterrows():
        action = row.get("predicted_action")
        hynix_ret = pd.to_numeric(row.get("hynix_return_pct"), errors="coerce")
        inverse_ret = pd.to_numeric(row.get("inverse_return_pct"), errors="coerce")
        buy_p = pd.to_numeric(row.get("buy_probability"), errors="coerce")
        sell_p = pd.to_numeric(row.get("sell_probability"), errors="coerce")
        if action == "BUY" and pd.notna(hynix_ret):
            valid += 1
            trades.append(hynix_ret)
            hit = 1.0 if hynix_ret > 0 else 0.0
            hits += hit
            if pd.notna(buy_p):
                forecast_probs.append(buy_p / 100.0)
                outcomes.append(hit)
        elif action == "INVERSE" and pd.notna(inverse_ret):
            valid += 1
            trades.append(inverse_ret)
            hit = 1.0 if inverse_ret > 0 else 0.0
            hits += hit
            if pd.notna(sell_p):
                forecast_probs.append(sell_p / 100.0)
                outcomes.append(hit)

    sample_size = len(attempted)
    valid_fraction = (valid / sample_size) if sample_size else 0.0

    gains = [t for t in trades if t > 0]
    losses = [abs(t) for t in trades if t < 0]
    gross_profit, gross_loss = sum(gains), sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    avg_return = (sum(trades) / len(trades)) if trades else None
    precision = (hits / len(trades) * 100.0) if trades else None
    brier_score = (
        sum((p - o) ** 2 for p, o in zip(forecast_probs, outcomes)) / len(forecast_probs)
        if forecast_probs else None
    )

    degrade_reasons = []
    if profit_factor is not None and profit_factor < PV2_DEGRADE_PF_MIN:
        degrade_reasons.append(f"Profit Factor {profit_factor:.2f} < {PV2_DEGRADE_PF_MIN}")
    if avg_return is not None and avg_return < 0:
        degrade_reasons.append(f"평균수익 {avg_return:.3f}% < 0")
    if brier_score is not None and brier_score > PV2_DEGRADE_BASELINE_BRIER:
        degrade_reasons.append(f"Brier Score {brier_score:.3f} > baseline {PV2_DEGRADE_BASELINE_BRIER}")
    if valid_fraction < PV2_DEGRADE_MIN_VALID_FRACTION:
        degrade_reasons.append(f"유효표본비율 {valid_fraction*100:.0f}% < {PV2_DEGRADE_MIN_VALID_FRACTION*100:.0f}%")

    result = {
        "sample_size": sample_size, "valid_sample_fraction": round(valid_fraction, 3),
        "profit_factor": profit_factor, "avg_return_pct": round(avg_return, 4) if avg_return is not None else None,
        "brier_score": round(brier_score, 4) if brier_score is not None else None,
        "precision": round(precision, 2) if precision is not None else None,
    }

    if degrade_reasons and sample_size >= 20:
        result["model_status"] = MODEL_STATUS_DEGRADED
        result["reason"] = "; ".join(degrade_reasons)
        return result

    if sample_size < 20:
        result["model_status"] = MODEL_STATUS_SHADOW
        result["reason"] = f"표본 {sample_size}건 — 최소 검증 표본 미달, SHADOW 유지"
        return result

    recovered = _check_recovery(df, horizon_minutes, now)
    if recovered:
        result["model_status"] = MODEL_STATUS_LIVE_VALIDATED if profit_factor and profit_factor >= 1.3 else MODEL_STATUS_ADVISORY
        result["reason"] = "성능 기준 충족(회복 조건 포함) — 주문 반영"
    else:
        result["model_status"] = MODEL_STATUS_ADVISORY
        result["reason"] = "기준선 충족(회복조건 미충족 또는 미평가) — ADVISORY"
    return result


def _check_recovery(df: pd.DataFrame, horizon_minutes: int, now: datetime) -> bool:
    sub = df[df["horizon_minutes"].astype(str) == str(horizon_minutes)].copy()
    sub["decision_timestamp"] = pd.to_datetime(sub["decision_timestamp"], errors="coerce")
    sub = sub.dropna(subset=["decision_timestamp"]).sort_values("decision_timestamp")
    if len(sub) < PV2_RECOVER_MIN_SAMPLES:
        return False
    last200 = sub.tail(PV2_RECOVER_MIN_SAMPLES)
    trades = []
    for _, row in last200.iterrows():
        action = row.get("predicted_action")
        hynix_ret = pd.to_numeric(row.get("hynix_return_pct"), errors="coerce")
        inverse_ret = pd.to_numeric(row.get("inverse_return_pct"), errors="coerce")
        if action == "BUY" and pd.notna(hynix_ret):
            trades.append(hynix_ret)
        elif action == "INVERSE" and pd.notna(inverse_ret):
            trades.append(inverse_ret)
    gains = [t for t in trades if t > 0]
    losses = [abs(t) for t in trades if t < 0]
    pf = (sum(gains) / sum(losses)) if sum(losses) > 0 else (float("inf") if gains else 0.0)
    if pf < PV2_RECOVER_PF_MIN:
        return False

    last200["date"] = last200["decision_timestamp"].dt.strftime("%Y%m%d")
    recent_dates = sorted(last200["date"].unique())[-3:]
    daily_avgs = []
    for d in recent_dates:
        day_rows = last200[last200["date"] == d]
        day_trades = []
        for _, row in day_rows.iterrows():
            action = row.get("predicted_action")
            hynix_ret = pd.to_numeric(row.get("hynix_return_pct"), errors="coerce")
            inverse_ret = pd.to_numeric(row.get("inverse_return_pct"), errors="coerce")
            if action == "BUY" and pd.notna(hynix_ret):
                day_trades.append(hynix_ret)
            elif action == "INVERSE" and pd.notna(inverse_ret):
                day_trades.append(inverse_ret)
        if day_trades:
            daily_avgs.append(sum(day_trades) / len(day_trades))
    if len(daily_avgs) < min(3, len(recent_dates)) or not daily_avgs:
        return False
    return all(a > 0 for a in daily_avgs)


# =============================================================================
# 모델 어댑터 — 각 하위 시스템의 결과를 표준 스키마(build_model_result)로 변환한다.
# =============================================================================

def _score_to_triple(score: float) -> tuple:
    """단일 0~100 방향성 점수(50=중립)를 (hynix_probability, inverse_probability,
    hold_probability) 3분할로 변환한다 — compute_buy_sell_hold_probability와 동일한
    변환 원리(conviction 클수록 hold 작아짐, 합계는 항상 100)."""
    score = max(0.0, min(100.0, score))
    conviction = min(100.0, abs(score - 50.0) * 2.0)
    hold = 100.0 - conviction
    if score >= 50.0:
        hynix, inverse = conviction, 0.0
    else:
        hynix, inverse = 0.0, conviction
    return round(hynix, 2), round(inverse, 2), round(hold, 2)


def model_result_from_active_fusion(decision_result: dict) -> Optional[dict]:
    """ACTIVE_FUSION(decide_active_strategy_action) 결과 → 표준 스키마.

    이미 수익 중인 검증된 전략이므로 항상 LIVE_VALIDATED로 취급한다(대체 대상 아님).
    """
    fusion = decision_result.get("fusion_result")
    if fusion is None:
        return None
    score = max(0.0, min(100.0, fusion.get("fusion_score", 50.0)))
    # fusion_score의 밴드(>=68 BUY, 58~67 시험진입, 50~57 HOLD, <50 INVERSE)가 이미
    # 결정적 신호이므로, 별도 hold 성분을 떼어내지 않고 score를 그대로 hynix_probability로
    # 사용한다(_score_to_triple의 conviction 압축을 적용하면 75점이 50% 확률로
    # 희석되어 이미 검증된 ACTIVE 신호가 과도하게 약해진다).
    hynix_p, inverse_p, hold_p = score, 100.0 - score, 0.0
    confidence = min(100.0, abs(score - 50.0) * 2.0 + 40.0)
    return build_model_result(
        model_name=MODEL_ACTIVE_FUSION, hynix_probability=hynix_p, inverse_probability=inverse_p,
        hold_probability=hold_p, confidence=confidence,
        recommended_position_pct=decision_result.get("recommended_position_pct", 0.0),
        data_quality=90.0, model_status=MODEL_STATUS_LIVE_VALIDATED,
        reasons=decision_result.get("reasons", []),
    )


def model_result_from_prediction_v2(probability: dict, decision_v2: dict, performance: dict) -> Optional[dict]:
    """Prediction AI V2(compute_buy_sell_hold_probability + decide_final_action_v2) 결과 →
    표준 스키마. model_status는 evaluate_prediction_v2_performance()가 결정한다(성능기반 자동감쇠)."""
    buy_p = probability.get("buy_probability")
    sell_p = probability.get("sell_probability")
    hold_p = probability.get("hold_probability")
    if buy_p is None or sell_p is None:
        return None
    confidence = max(buy_p, sell_p)
    status = performance.get("model_status", MODEL_STATUS_SHADOW)
    sample_size = performance.get("sample_size", 0)
    data_quality = (performance.get("valid_sample_fraction", 0.5) * 100.0) if sample_size > 0 else 50.0
    reasons = [f"final_action_v2={decision_v2.get('final_action_v2')}"]
    if performance.get("reason"):
        reasons.append(str(performance["reason"]))
    return build_model_result(
        model_name=MODEL_PREDICTION_V2, hynix_probability=buy_p, inverse_probability=sell_p,
        hold_probability=hold_p if hold_p is not None else max(0.0, 100.0 - buy_p - sell_p),
        confidence=confidence,
        recommended_position_pct=(50.0 if decision_v2.get("final_action_v2") in ("BUY", "INVERSE") else 0.0),
        data_quality=data_quality, model_status=status, reasons=reasons,
    )


def model_result_from_cycle_ai(cycle_result: dict, validated: bool = False) -> Optional[dict]:
    """Cycle & Turning Point AI 결과 → 표준 스키마. `validated`는 명세(Cycle Detector
    17절)가 요구하는 최소 5거래일 Shadow 검증 완료 여부 — 호출부가 state 플래그로 넘긴다."""
    turning = cycle_result.get("turning_point") or {}
    up_p = turning.get("up_turn_probability_3m")
    down_p = turning.get("down_turn_probability_3m")
    if up_p is None or down_p is None:
        return None
    confidence = cycle_result.get("cycle_confidence", 50.0)
    status = MODEL_STATUS_LIVE_VALIDATED if validated else MODEL_STATUS_SHADOW
    return build_model_result(
        model_name=MODEL_CYCLE_AI, hynix_probability=up_p, inverse_probability=down_p,
        hold_probability=max(0.0, 100.0 - up_p - down_p), confidence=confidence,
        recommended_position_pct=cycle_result.get("recommended_position_pct", 0.0),
        data_quality=confidence, model_status=status,
        reasons=(cycle_result.get("reasons") or [])[:3],
    )


def model_result_from_early_prediction(momentum: dict, turning_point: Optional[dict] = None) -> Optional[dict]:
    """Early Prediction / Momentum Inflection 결과 → 표준 스키마. Cycle AI(C)와 같은
    원천 데이터(momentum)를 쓰지만 독립된 방향성 신호(가속도 자체)로 별도 모델 취급한다."""
    up_accel = momentum.get("momentum_acceleration_up")
    down_accel = momentum.get("momentum_acceleration_down")
    if up_accel is None or down_accel is None:
        return None
    score = 50.0 + (up_accel - down_accel) / 2.0
    hynix_p, inverse_p, hold_p = _score_to_triple(score)
    confidence = max(up_accel, down_accel)
    tp_confidence = (turning_point or {}).get("confidence")
    data_quality = 70.0 if (tp_confidence or 0) > 0 else 55.0
    return build_model_result(
        model_name=MODEL_EARLY_PREDICTION, hynix_probability=hynix_p, inverse_probability=inverse_p,
        hold_probability=hold_p, confidence=confidence,
        recommended_position_pct=15.0 if max(hynix_p, inverse_p) >= 60.0 else 0.0,
        data_quality=data_quality, model_status=MODEL_STATUS_ADVISORY,
        reasons=[f"momentum_accel_up={up_accel:.0f} momentum_accel_down={down_accel:.0f}"],
    )


def model_result_from_micron_proxy(micron_proxy: dict) -> Optional[dict]:
    """Micron Proxy / External Semiconductor 결과 → 표준 스키마. stale/저confidence면
    ADVISORY가 아니라 SHADOW로 강등해 가중치를 절반으로 줄인다(섹션 13)."""
    score = micron_proxy.get("effective_micron_score")
    if score is None:
        return None
    confidence = micron_proxy.get("micron_data_confidence", 50.0)
    is_stale = bool(micron_proxy.get("is_stale"))
    if not is_stale and micron_proxy.get("calculated_at"):
        try:
            age = (datetime.now() - datetime.fromisoformat(str(micron_proxy.get("calculated_at")))).total_seconds() / 60.0
            micron_proxy["age_minutes"] = round(max(0.0, age), 2)
            is_stale = age > 15.0
        except Exception:
            pass
    if is_stale or (micron_proxy.get("age_minutes") is not None and micron_proxy.get("age_minutes") > 15.0):
        confidence = 0.0
    hynix_p, inverse_p, hold_p = _score_to_triple(score)
    status = MODEL_STATUS_DEGRADED if confidence <= 0.0 else (MODEL_STATUS_ADVISORY if confidence >= 60.0 else MODEL_STATUS_SHADOW)
    return build_model_result(
        model_name=MODEL_MICRON_PROXY, hynix_probability=hynix_p, inverse_probability=inverse_p,
        hold_probability=hold_p, confidence=confidence, recommended_position_pct=0.0,
        data_quality=confidence, model_status=status,
        reasons=[f"score_source={micron_proxy.get('micron_score_source')}", f"age={micron_proxy.get('age_minutes')} stale={is_stale}"],
    )


# =============================================================================
# FusionDecision — 최종 실행 판단 객체(섹션 1 출력 스키마 + blocking_reason)
# =============================================================================

_DEFAULT_EXPECTED_PROFIT_PCT = 3.0  # DynamicExitEngine NORMAL 프로필 tp_pct와 동일 근사치
_DEFAULT_EXPECTED_LOSS_PCT = 1.5    # DynamicExitEngine NORMAL 프로필 sl_pct와 동일 근사치


def _model_diagnostics(model_results: dict, weights: dict) -> list:
    """로그/UI용 — 모델별 방향·확률·가중치·데이터 신선도(요구사항 6절).

    weights는 반드시 fuse_model_results()가 실제로 사용한 가중치(fused["weights"])를
    받아야 한다 — 여기서 renormalize_weights()를 다시 호출하면 ACTIVE_FUSION 중립
    감쇠(_dampen_neutral_active_fusion) 적용 전 값을 보여주게 되어 실제 융합에 쓰인
    가중치와 화면 표시가 어긋난다."""
    rows = []
    for name, r in model_results.items():
        if r is None:
            rows.append({"model": name, "available": False})
            continue
        rows.append({
            "model": name, "available": True,
            "action": _implied_action(r), "hynix_probability": r["hynix_probability"],
            "inverse_probability": r["inverse_probability"], "hold_probability": r["hold_probability"],
            "confidence": r["confidence"], "weight_pct": round(weights.get(name, 0.0) * 100.0, 1),
            "data_quality": r["data_quality"], "model_status": r["model_status"],
        })
    return rows


def build_fusion_decision(
    fused: dict, conflict: dict, final_pct_result: dict, expected_move_3m: Optional[float],
    expected_move_5m: Optional[float], expected_value: float, final_action: str, target_symbol: Optional[str],
    executable: bool, blocking_reason: Optional[str], threshold_used: float,
    *, original_threshold: float = _ENTRY_MIN_PROBABILITY, threshold_relief: float = 0.0,
    relief_reasons: Optional[list] = None, model_results: Optional[dict] = None,
    disagreement_index: float = 0.0, disagreement_override: Optional[dict] = None,
    strong_conflict: bool = False, entry_type: Optional[str] = None,
    consecutive_loss_note: Optional[str] = None, frequency_state: Optional[dict] = None,
    orders_today_count: int = 0, live_hynix_trend: Optional[dict] = None,
) -> dict:
    reasons = fused.get("reasons", []) + conflict.get("notes", [])
    if relief_reasons:
        reasons = reasons + list(relief_reasons)
    if disagreement_override:
        reasons = reasons + [disagreement_override["reason"]]
    if consecutive_loss_note:
        reasons = reasons + [consecutive_loss_note]

    return {
        "fused_hynix_probability": fused["fused_hynix_probability"],
        "fused_inverse_probability": fused["fused_inverse_probability"],
        "fused_hold_probability": fused["fused_hold_probability"],
        "fused_confidence": fused["fused_confidence"],
        "dominant_model": fused["dominant_model"], "model_agreement": fused["model_agreement"],
        "weights": fused["weights"],
        "final_action": final_action, "symbol": target_symbol,
        "target_position_pct": final_pct_result["final_pct"],
        "probability_ladder_pct": final_pct_result["probability_ladder_pct"],
        "ev_ladder_pct": final_pct_result["ev_ladder_pct"],
        "expected_move_3m": expected_move_3m, "expected_move_5m": expected_move_5m,
        "expected_value": expected_value,
        "entry_threshold_used": threshold_used, "entry_threshold_original": original_threshold,
        "threshold_relief_applied": round(threshold_relief, 2),
        "conflict_notes": conflict.get("notes", []), "reasons": reasons,
        "executable": executable, "blocking_reason": blocking_reason,
        # ── 요구사항 6절: 로그/UI 진단 필드 ──────────────────────────────────
        "model_diagnostics": _model_diagnostics(model_results, fused.get("weights", {})) if model_results is not None else [],
        "disagreement_index": disagreement_index,
        "disagreement_override_used": bool(disagreement_override),
        "disagreement_override_detail": disagreement_override,
        "strong_signal_conflict": strong_conflict,
        "entry_type": entry_type,
        "orders_today_count": orders_today_count,
        "round_trips_today": (frequency_state or {}).get("round_trips_today", 0),
        "max_daily_round_trips": _load_fusion_v2_config()["max_daily_round_trips"],
        "daily_target_trades": [DAILY_TARGET_TRADES_MIN, DAILY_TARGET_TRADES_MAX],
        "live_hynix_trend": live_hynix_trend or {},
        "top_decision_factors": (live_hynix_trend or {}).get("top_factors", []) + reasons[:3],
    }


class HynixAdaptiveFusionEngine:
    """5개 모델을 융합해 최종 FusionDecision을 계산한다. 이 클래스는 주문을 실행하지
    않는다 — 반환값의 executable/blocking_reason만 참고해 호출부가 브로커를 호출한다."""

    def decide(
        self, *, now: datetime, active_decision_result: dict, prediction_v2_probability: dict,
        prediction_v2_decision: dict, prediction_v2_performance: dict, cycle_result: dict,
        cycle_ai_validated: bool, micron_proxy: Optional[dict],
        held_symbol: Optional[str], position_conflict: bool, data_ok: bool, price_is_stale: bool,
        daily_return_pct: Optional[float], orders_today_count: int,
        hold_tracker: dict, whipsaw_state: dict, consecutive_stop_losses: int,
        frequency_state: Optional[dict] = None,
        live_hynix_trend: Optional[dict] = None,
        expected_profit_pct: float = _DEFAULT_EXPECTED_PROFIT_PCT,
        expected_loss_pct: float = _DEFAULT_EXPECTED_LOSS_PCT,
        estimated_fees_pct: float = 0.015, estimated_slippage_pct: float = 0.02,
        estimated_spread_pct: float = 0.0,
    ) -> dict:
        frequency_state = frequency_state or default_frequency_state()
        cfg = _load_fusion_v2_config()
        momentum = (cycle_result.get("momentum") or {})
        turning_point = (cycle_result.get("turning_point") or {})
        cycle_phase = cycle_result.get("cycle_phase")

        model_results = {
            MODEL_ACTIVE_FUSION: model_result_from_active_fusion(active_decision_result),
            MODEL_PREDICTION_V2: model_result_from_prediction_v2(
                prediction_v2_probability, prediction_v2_decision, prediction_v2_performance,
            ),
            MODEL_CYCLE_AI: model_result_from_cycle_ai(cycle_result, validated=cycle_ai_validated),
            MODEL_EARLY_PREDICTION: model_result_from_early_prediction(momentum, turning_point),
            MODEL_MICRON_PROXY: model_result_from_micron_proxy(micron_proxy) if micron_proxy else None,
        }

        fused = fuse_model_results(model_results)
        disagreement_index = round(100.0 - fused["model_agreement"], 1)
        strong_conflict = detect_strong_signal_conflict(model_results)

        active_action = _map_active_strategy_action(active_decision_result.get("action"))
        pv2_action = None
        pv2_r = model_results.get(MODEL_PREDICTION_V2)
        if pv2_r is not None:
            pv2_action = _implied_action(pv2_r)

        # ── expected_move / dom_prob — 문턱완화(threshold relief) 계산에 필요하므로
        # 사다리 적용보다 먼저 계산한다 ──────────────────────────────────────────
        dom_prob = max(fused["fused_hynix_probability"], fused["fused_inverse_probability"])
        expected_move_3m = round(abs(turning_point.get("up_turn_probability_3m", 50.0) - turning_point.get("down_turn_probability_3m", 50.0)) / 100.0, 4)
        expected_move_5m = round(abs(turning_point.get("up_turn_probability_5m", 50.0) - turning_point.get("down_turn_probability_5m", 50.0)) / 100.0, 4)

        # ── HOLD 완화 — 기존(HOLD 연속 발생) + 신규(시간대별, 요구사항 3절) 합산 ──
        opposite_prob = fused["fused_inverse_probability"] if fused["final_action"] == ACTION_HYNIX else fused["fused_hynix_probability"]
        relief_result = compute_threshold_relief(
            hold_tracker, cycle_phase, fused["fused_confidence"], opposite_prob, expected_move_5m,
            model_results.get(MODEL_ACTIVE_FUSION, {}).get("data_quality", 90.0) if model_results.get(MODEL_ACTIVE_FUSION) else 90.0,
            consecutive_stop_losses,
        )
        time_relief_result = time_based_threshold_relief(now, orders_today_count, daily_return_pct)
        total_relief = relief_result["relief"] + time_relief_result["relief"]
        threshold = max(TRIAL_ENTRY_MIN_THRESHOLD, cfg["entry_min_probability"] - total_relief)

        # 완화된 문턱을 사다리 바닥에도 그대로 반영한다(entry_gate_ok 표시에만 쓰이고
        # 실제 진입비중에는 반영되지 않던 버그 수정 — position_pct_from_probability_ladder
        # 주석 참조).
        base_pct = position_pct_from_probability_ladder(dom_prob, floor_relief=total_relief)
        conflict = apply_conflict_resolution(fused, active_action, pv2_action, cycle_phase, base_pct)

        expected_value = calculate_expected_value(
            dom_prob, expected_profit_pct, expected_loss_pct, estimated_fees_pct, estimated_slippage_pct,
        )

        final_pct_result = calculate_final_position_pct(conflict["position_pct"], expected_value)

        # ── Whipsaw 완화 ─────────────────────────────────────────────────────
        whipsaw_result = apply_whipsaw_dampening(final_pct_result["final_pct"], threshold, whipsaw_state, now)
        final_pct = whipsaw_result["position_pct"]
        threshold = whipsaw_result["threshold"]

        # ── 리스크 사다리(섹션 16, 손익 기반 비중/문턱 조정) ────────────────────
        risk = adaptive_fusion_daily_risk_ladder(daily_return_pct)
        final_pct = min(final_pct, risk["max_position_pct"])
        threshold += risk["threshold_add"]

        # ── 연속 손절 비중 축소(요구사항 4절 — 2연속 손절 시 다음 진입 비중 절반) ──
        consecutive_loss_note = None
        if consecutive_stop_losses >= cfg["consecutive_loss_halve_threshold"] and final_pct > 0:
            final_pct = round(final_pct * 0.5, 2)
            consecutive_loss_note = f"연속손절 {consecutive_stop_losses}회 — 진입 비중 50% 축소"

        final_action = fused["final_action"]
        target_symbol = None
        if final_action == ACTION_HYNIX:
            target_symbol = "000660"
        elif final_action == ACTION_INVERSE:
            target_symbol = "0197X0"

        live_trend_note = None
        if live_hynix_trend and live_hynix_trend.get("hynix_uptrend_confirmed"):
            streak = int(live_hynix_trend.get("hynix_uptrend_streak", 1) or 1)
            if final_action == ACTION_INVERSE:
                if streak >= 2:
                    final_action, target_symbol = ACTION_HYNIX, "000660"
                    final_pct = min(final_pct if final_pct > 0 else 25.0, 45.0)
                    live_trend_note = "live Hynix uptrend confirmed 2x: INVERSE blocked and switched to HYNIX"
                else:
                    final_action, target_symbol, final_pct = ACTION_HOLD, None, 0.0
                    live_trend_note = "live Hynix uptrend confirmed: INVERSE new entry blocked"
        elif live_hynix_trend and live_hynix_trend.get("hynix_downtrend_confirmed"):
            streak = int(live_hynix_trend.get("hynix_downtrend_streak", 1) or 1)
            if final_action == ACTION_HYNIX and streak >= 2:
                final_action, target_symbol = ACTION_INVERSE, "0197X0"
                final_pct = min(final_pct if final_pct > 0 else 25.0, 45.0)
                live_trend_note = "live Hynix downtrend confirmed 2x: switched to INVERSE"

        # ── 모델 불일치 예외 진입(요구사항 2절) — 정규 사다리가 0%일 때만 시도한다.
        # 강한 신호 충돌(strong_conflict)이면 애초에 시도하지 않는다(HOLD가 우선).
        disagreement_override = None
        used_override = False
        if final_pct <= 0 and not strong_conflict:
            disagreement_override = evaluate_disagreement_override(model_results)
            if disagreement_override is not None:
                final_action = disagreement_override["action"]
                target_symbol = disagreement_override["target_symbol"]
                final_pct = disagreement_override["position_pct"]
                used_override = True
                # EV는 leader 모델의 확신도를 기준으로 재계산한다 — 희석된 fused
                # dom_prob 그대로 쓰면 예외 진입의 취지(고확신 모델 존중)와 어긋난다.
                expected_value = calculate_expected_value(
                    disagreement_override["leader_confidence"], expected_profit_pct, expected_loss_pct,
                    estimated_fees_pct, estimated_slippage_pct,
                )

        executable = True
        blocking_reason = None

        if strong_conflict:
            executable, blocking_reason = False, "하이닉스/인버스 강신호(>=70%) 동시 충돌 — HOLD"
            final_action, target_symbol, final_pct = ACTION_HOLD, None, 0.0
        elif not data_ok:
            executable, blocking_reason = False, "데이터 오류 — 신규 진입 금지"
        elif price_is_stale:
            executable, blocking_reason = False, "가격 데이터 stale"
        elif position_conflict:
            executable, blocking_reason = False, "Broker/Position 불일치"
        elif now.strftime("%H:%M") >= "14:50":
            # 플랫폼 공통 규칙(요구사항: 14:50 이후 신규매수 금지)과 일치시킨다 —
            # 과거 "15:00"이었던 것은 ENHANCED_LEGACY/강제청산 경로의 14:50 컷오프와
            # 어긋나는 별도 버그였다.
            executable, blocking_reason = False, "14:50 이후 — 신규 진입 금지"
        elif not risk["entries_allowed"]:
            executable, blocking_reason = False, f"일 손익 리스크 사다리로 신규진입 중단(daily_return={daily_return_pct})"
        elif consecutive_stop_losses >= cfg["consecutive_loss_block_threshold"]:
            executable, blocking_reason = False, f"연속손절 {consecutive_stop_losses}회(>={cfg['consecutive_loss_block_threshold']}) — 신규진입 중단"
        elif conflict["force_hold"] and not used_override:
            executable, blocking_reason = False, "ACTIVE/Prediction V2 반대방향 확률차<10%p — HOLD"
        elif final_action == ACTION_HOLD or not target_symbol:
            executable, blocking_reason = False, None
        elif final_pct <= 0:
            executable, blocking_reason = False, "target_position_pct<=0"
        elif expected_value <= 0:
            executable, blocking_reason = False, f"expected_value {expected_value:.4f}<=0"
        elif estimated_spread_pct > 0 and estimated_spread_pct > expected_value:
            executable, blocking_reason = False, "예상 spread/slippage가 기대수익보다 큼"
        elif not used_override:
            gate_reason = entry_gate_ok(dom_prob, opposite_prob, fused["fused_confidence"], expected_move_5m)
            if gate_reason and dom_prob < threshold:
                executable, blocking_reason = False, gate_reason

        # ── 거래빈도 제한(요구사항 3절) — 신호가 유효해도 과매매는 막는다.
        # 강제로 거래를 "만들어내지" 않으므로 이 체크는 executable=True일 때만 의미가 있다.
        if executable and target_symbol:
            freq_block = check_frequency_limits(frequency_state, final_action, now)
            if freq_block:
                executable, blocking_reason = False, freq_block

        entry_type = None
        if executable:
            entry_type = "EXPLORATORY" if (final_pct <= cfg["exploratory_max_pct"] or used_override) else "NORMAL"

        final_pct_result = {**final_pct_result, "final_pct": round(final_pct, 2)}
        return build_fusion_decision(
            fused, conflict, final_pct_result, expected_move_3m, expected_move_5m, expected_value,
            final_action, target_symbol, executable, blocking_reason, threshold,
            original_threshold=cfg["entry_min_probability"], threshold_relief=total_relief,
            relief_reasons=(relief_result.get("blocked_reasons") or []) + time_relief_result.get("reasons", []) + ([live_trend_note] if live_trend_note else []),
            model_results=model_results, disagreement_index=disagreement_index,
            disagreement_override=disagreement_override, strong_conflict=strong_conflict,
            entry_type=entry_type, consecutive_loss_note=consecutive_loss_note,
            frequency_state=frequency_state, orders_today_count=orders_today_count,
            live_hynix_trend=live_hynix_trend,
        )
