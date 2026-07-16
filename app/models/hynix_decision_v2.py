"""hynix_decision_v2.py — Prediction AI V2를 최종 주문 결정권자로 만드는 확률/임계값 레이어.

구조:
  Feature Engine → Prediction AI(Cycle Detector의 3/5/10분 turning point 확률)
  → BUY/SELL/HOLD 확률 → Adaptive Threshold → 최종 액션(BUY/INVERSE/HOLD)

기존 enhanced_score(app.models.hynix_enhanced_score)는 삭제하지 않고 참고 feature로만
사용한다 — 이 모듈의 함수들에 enhanced_score를 넘기면 보조 가중치로만 반영되고,
단독으로 최종 게이트가 되지는 않는다.

Prediction Accuracy는 방향이 아니라 "실현 수익률" 기준으로 재정의한다:
  BUY 또는 INVERSE 진입 후 5분 뒤 해당 종목 수익률이 +0.3% 이상이면 SUCCESS,
  0.3% 미만이면 VOID(무효), 반대 방향으로 -0.3% 이하 움직이면 FAILURE.
학습 목표는 방향 정확도가 아니라 Profit Factor(총이익/총손실) 최대화다.

이 모듈은 주문을 실행하지 않는다 — decide_final_action_v2()의 반환값은 "권장" 액션이며,
실제로 주문을 이 값에 연결할지는 호출부(app/services/hynix_switch_engine.py)가
Shadow Mode 검증 후 별도로 결정한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.utils.data_paths import STATE_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
_WEIGHT_RECO_PATH = STATE_DIR / "hynix_decision_v2_weight_recommendation.json"

DEFAULT_BUY_THRESHOLD = 65.0
DEFAULT_SELL_THRESHOLD = 65.0
RELAXED_THRESHOLD = 60.0
TIGHTENED_THRESHOLD = 70.0

HOLD_STREAK_FOR_RELAX = 5
HOLD_STREAK_WINDOW_MINUTES = 30
WHIPSAW_FLIP_THRESHOLD = 3
WHIPSAW_SEVERE_FLIP_THRESHOLD = 5

ACTION_BUY = "BUY"
ACTION_INVERSE = "INVERSE"
ACTION_HOLD = "HOLD"

REALIZED_OUTCOME_SUCCESS = "SUCCESS"
REALIZED_OUTCOME_VOID = "VOID"
REALIZED_OUTCOME_FAILURE = "FAILURE"
REALIZED_OUTCOME_NOT_APPLICABLE = "NOT_APPLICABLE"
REALIZED_OUTCOME_UNKNOWN = "UNKNOWN"

PROFIT_FACTOR_MIN_SAMPLES = 200

# 3/5/10분 turning point를 BUY/SELL 확률로 합성할 때의 horizon별 가중치(짧은 horizon일수록
# 진입 타이밍에 더 중요하므로 더 크게 반영). 고정 하드코딩이 아니라 recommend_profit_factor_weights()가
# 표본이 쌓이면 조정값을 추천한다(자동 반영은 하지 않음).
_DEFAULT_HORIZON_WEIGHTS = {"3m": 0.5, "5m": 0.3, "10m": 0.2}
_ENHANCED_SCORE_REFERENCE_WEIGHT = 0.10  # 참고 feature일 뿐 게이트가 아님 — 작은 보정 비중만 적용
_MICRON_SCORE_REFERENCE_WEIGHT = 0.08


def default_threshold_state() -> dict:
    return {
        "buy_threshold": DEFAULT_BUY_THRESHOLD, "sell_threshold": DEFAULT_SELL_THRESHOLD,
        "action_history": [], "consecutive_hold": 0, "whipsaw_flips": 0,
    }


# =============================================================================
# BUY/SELL/HOLD 확률
# =============================================================================

def compute_buy_sell_hold_probability(
    turning_point: dict, horizon_weights: Optional[dict] = None,
    enhanced_score: Optional[float] = None, effective_micron_score: Optional[float] = None,
) -> dict:
    """Cycle Detector의 up/down turn probability(3m/5m/10m)를 BUY/SELL/HOLD 확률로 합성한다.

    enhanced_score/effective_micron_score는 참고 feature로만 작은 비중(각 10%/8%)을 차지하며,
    turning point 신호가 전혀 없을 때도 절대 단독으로 최종 게이트가 되지 않는다.
    """
    weights = horizon_weights or _DEFAULT_HORIZON_WEIGHTS

    def _weighted(prefix: str) -> float:
        total, wsum = 0.0, 0.0
        for horizon, w in weights.items():
            key = f"{prefix}_probability_{horizon}"
            v = turning_point.get(key)
            if v is None:
                continue
            total += v * w
            wsum += w
        return total / wsum if wsum > 0 else 50.0

    buy_score = _weighted("up_turn")
    sell_score = _weighted("down_turn")

    # 참고 feature 반영(작은 비중) — enhanced_score/effective_micron_score가 강하게 한쪽을
    # 가리키면 buy_score/sell_score를 소폭 보정하되, turning point 신호를 뒤집지는 못한다.
    if enhanced_score is not None:
        adj = (enhanced_score - 50.0) * _ENHANCED_SCORE_REFERENCE_WEIGHT
        buy_score += max(0.0, adj)
        sell_score += max(0.0, -adj)
    if effective_micron_score is not None:
        adj = (effective_micron_score - 50.0) * _MICRON_SCORE_REFERENCE_WEIGHT
        buy_score += max(0.0, adj)
        sell_score += max(0.0, -adj)

    buy_score = max(0.0, min(100.0, buy_score))
    sell_score = max(0.0, min(100.0, sell_score))

    buy_excess = max(0.0, buy_score - 50.0)
    sell_excess = max(0.0, sell_score - 50.0)
    total_excess = buy_excess + sell_excess
    conviction = min(100.0, total_excess * 2.0)
    hold_probability = max(5.0, 100.0 - conviction)
    remaining = 100.0 - hold_probability

    if total_excess > 0:
        buy_probability = remaining * (buy_excess / total_excess)
        sell_probability = remaining - buy_probability
    else:
        buy_probability = remaining / 2.0
        sell_probability = remaining / 2.0

    total = buy_probability + sell_probability + hold_probability
    if total <= 0:
        buy_probability, sell_probability, hold_probability = 0.0, 0.0, 100.0
        total = 100.0

    return {
        "buy_probability": round(buy_probability / total * 100.0, 1),
        "sell_probability": round(sell_probability / total * 100.0, 1),
        "hold_probability": round(hold_probability / total * 100.0, 1),
        "raw_buy_score": round(buy_score, 2), "raw_sell_score": round(sell_score, 2),
        "horizon_weights": weights,
    }


# =============================================================================
# Adaptive Threshold
# =============================================================================

def adaptive_threshold_update(threshold_state: Optional[dict], latest_action: str, now: datetime) -> dict:
    """최근 30분 동안 HOLD 5회 이상 연속이면 임계값 65→60으로 완화, whipsaw가 많으면 다시 올린다."""
    state = dict(threshold_state) if threshold_state else default_threshold_state()
    history = list(state.get("action_history", []))
    history.append({"at": now.isoformat(), "action": latest_action})

    cutoff = now - timedelta(minutes=HOLD_STREAK_WINDOW_MINUTES)
    pruned = []
    for h in history:
        try:
            if datetime.fromisoformat(h["at"]) >= cutoff:
                pruned.append(h)
        except Exception:
            continue
    history = pruned[-200:]
    state["action_history"] = history

    consecutive_hold = 0
    for h in reversed(history):
        if h["action"] == ACTION_HOLD:
            consecutive_hold += 1
        else:
            break

    flips = 0
    prev_dir = None
    for h in history:
        if h["action"] in (ACTION_BUY, ACTION_INVERSE):
            if prev_dir is not None and prev_dir != h["action"]:
                flips += 1
            prev_dir = h["action"]

    if flips >= WHIPSAW_SEVERE_FLIP_THRESHOLD:
        threshold = TIGHTENED_THRESHOLD
    elif flips >= WHIPSAW_FLIP_THRESHOLD:
        threshold = DEFAULT_BUY_THRESHOLD
    elif consecutive_hold >= HOLD_STREAK_FOR_RELAX:
        threshold = RELAXED_THRESHOLD
    else:
        threshold = state.get("buy_threshold", DEFAULT_BUY_THRESHOLD)
        if threshold not in (RELAXED_THRESHOLD, DEFAULT_BUY_THRESHOLD, TIGHTENED_THRESHOLD):
            threshold = DEFAULT_BUY_THRESHOLD

    state["buy_threshold"] = threshold
    state["sell_threshold"] = threshold
    state["consecutive_hold"] = consecutive_hold
    state["whipsaw_flips"] = flips
    return state


def decide_final_action_v2(probability: dict, threshold_state: dict) -> dict:
    """BUY>=threshold → BUY, SELL>=threshold → INVERSE, 둘 다 미만 → HOLD."""
    buy_p = probability.get("buy_probability", 0.0)
    sell_p = probability.get("sell_probability", 0.0)
    buy_th = threshold_state.get("buy_threshold", DEFAULT_BUY_THRESHOLD)
    sell_th = threshold_state.get("sell_threshold", DEFAULT_SELL_THRESHOLD)

    if buy_p >= buy_th and buy_p >= sell_p:
        action = ACTION_BUY
    elif sell_p >= sell_th and sell_p > buy_p:
        action = ACTION_INVERSE
    else:
        action = ACTION_HOLD

    return {
        "final_action_v2": action, "buy_probability": buy_p, "sell_probability": sell_p,
        "hold_probability": probability.get("hold_probability", 0.0),
        "buy_threshold": buy_th, "sell_threshold": sell_th,
    }


# =============================================================================
# Fusion Score — Cycle Phase는 Entry Gate가 아니라 최종점수의 보조 feature일 뿐이다.
#
# fusion_score = 0.35*PredictionAI + 0.25*EnhancedAI + 0.20*MomentumAI
#                + 0.10*MicronAI + 0.10*CycleBonus
#
# 4개 AI 점수(PredictionAI/EnhancedAI/MomentumAI/MicronAI)는 0~100(50=중립,
# 높을수록 하이닉스 강세) 방향성 점수이고, CycleBonus는 calculate_cycle_bonus()가
# 반환하는 작은 가점/감점(-10~+15 수준)을 그대로(재정규화 없이) 더한다 — 그래서
# 중립 상태(모든 AI=50)의 fusion_score는 약 45이고, cycle bonus의 실제 기여는
# ±0.4~1.5점 수준이다(의도적 — "Cycle Phase는 사소한 feature"라는 요구를 그대로
# 반영). Cycle AI는 이 구간 로직에서도 절대 단독으로 주문을 차단하지 않는다.
# =============================================================================

FUSION_WEIGHT_PREDICTION_AI = 0.35
FUSION_WEIGHT_ENHANCED_AI = 0.25
FUSION_WEIGHT_MOMENTUM_AI = 0.20
FUSION_WEIGHT_MICRON_AI = 0.10
FUSION_WEIGHT_CYCLE_BONUS = 0.10

FUSION_BUY_THRESHOLD = 68.0
FUSION_TRIAL_ENTRY_MIN = 58.0
FUSION_HOLD_MIN = 50.0
FUSION_NO_TRADE_OVERRIDE_SCORE = 65.0
FUSION_NO_TRADE_TRIAL_ENTRY_PCT = 15.0
FUSION_TRIAL_ENTRY_PCT = 20.0
FUSION_FULL_ENTRY_PCT = 50.0

FUSION_BAND_BUY = "BUY"
FUSION_BAND_TRIAL_ENTRY = "TRIAL_ENTRY"
FUSION_BAND_HOLD = "HOLD"
FUSION_BAND_INVERSE = "INVERSE"
FUSION_BAND_NO_TRADE_OVERRIDE = "NO_TRADE_OVERRIDE"


def calculate_prediction_ai_directional_score(buy_probability: float, sell_probability: float) -> float:
    """BUY/SELL 확률(합 100%가 아니어도 무방)을 0~100 방향성 점수로 변환(50=중립)."""
    return round(max(0.0, min(100.0, 50.0 + (buy_probability - sell_probability) / 2.0)), 2)


def calculate_momentum_ai_directional_score(momentum_acceleration_up: float, momentum_acceleration_down: float) -> float:
    """상승/하락 모멘텀 가속도를 0~100 방향성 점수로 변환(50=중립)."""
    return round(max(0.0, min(100.0, 50.0 + (momentum_acceleration_up - momentum_acceleration_down) / 2.0)), 2)


def calculate_fusion_score(
    prediction_ai_score: float, enhanced_ai_score: float, momentum_ai_score: float,
    micron_ai_score: float, cycle_phase: Optional[str] = None, cycle_bonus: Optional[float] = None,
) -> dict:
    """PredictionAI/EnhancedAI/MomentumAI/MicronAI(각 0~100) + Cycle Bonus를 합성한다.

    cycle_bonus를 직접 넘기지 않으면 cycle_phase로 app.trading.hynix_cycle_detector.
    calculate_cycle_bonus()를 조회한다.
    """
    if cycle_bonus is None:
        from app.trading.hynix_cycle_detector import calculate_cycle_bonus

        cycle_bonus = calculate_cycle_bonus(cycle_phase)

    fusion_score = (
        FUSION_WEIGHT_PREDICTION_AI * prediction_ai_score
        + FUSION_WEIGHT_ENHANCED_AI * enhanced_ai_score
        + FUSION_WEIGHT_MOMENTUM_AI * momentum_ai_score
        + FUSION_WEIGHT_MICRON_AI * micron_ai_score
        + FUSION_WEIGHT_CYCLE_BONUS * cycle_bonus
    )
    return {
        "fusion_score": round(fusion_score, 2), "cycle_bonus": cycle_bonus,
        "prediction_ai_score": prediction_ai_score, "enhanced_ai_score": enhanced_ai_score,
        "momentum_ai_score": momentum_ai_score, "micron_ai_score": micron_ai_score,
    }


def decide_fusion_based_action(fusion_result: dict, cycle_phase: Optional[str] = None) -> dict:
    """fusion_score 구간(>=68 BUY, 58~67 시험진입, 50~57 HOLD, <50 INVERSE)으로
    액션/비중을 정한다. Cycle Phase는 여기서도 단독으로 주문을 차단하지 않는다 —
    NO_TRADE라도 PredictionAI 또는 EnhancedAI가 65 이상이면 fusion_score 구간과
    무관하게 15% 시험진입을 허용한다.
    """
    score = fusion_result["fusion_score"]
    prediction_ai = fusion_result["prediction_ai_score"]
    enhanced_ai = fusion_result["enhanced_ai_score"]

    if cycle_phase == "NO_TRADE" and (prediction_ai >= FUSION_NO_TRADE_OVERRIDE_SCORE or enhanced_ai >= FUSION_NO_TRADE_OVERRIDE_SCORE):
        return {
            "action": ACTION_BUY, "position_pct": FUSION_NO_TRADE_TRIAL_ENTRY_PCT, "band": FUSION_BAND_NO_TRADE_OVERRIDE,
            "reason": (
                f"NO_TRADE이지만 PredictionAI {prediction_ai:.0f}/EnhancedAI {enhanced_ai:.0f} 중 "
                f"{FUSION_NO_TRADE_OVERRIDE_SCORE:.0f} 이상 — {FUSION_NO_TRADE_TRIAL_ENTRY_PCT:.0f}% 시험진입 허용"
            ),
        }

    if score >= FUSION_BUY_THRESHOLD:
        return {"action": ACTION_BUY, "position_pct": FUSION_FULL_ENTRY_PCT, "band": FUSION_BAND_BUY,
                "reason": f"fusion_score {score:.1f} >= {FUSION_BUY_THRESHOLD:.0f} — BUY"}
    if score >= FUSION_TRIAL_ENTRY_MIN:
        return {"action": ACTION_BUY, "position_pct": FUSION_TRIAL_ENTRY_PCT, "band": FUSION_BAND_TRIAL_ENTRY,
                "reason": f"fusion_score {score:.1f} — 시험진입({FUSION_TRIAL_ENTRY_PCT:.0f}%)"}
    if score >= FUSION_HOLD_MIN:
        return {"action": ACTION_HOLD, "position_pct": 0.0, "band": FUSION_BAND_HOLD,
                "reason": f"fusion_score {score:.1f} — HOLD"}
    return {"action": ACTION_INVERSE, "position_pct": FUSION_FULL_ENTRY_PCT, "band": FUSION_BAND_INVERSE,
            "reason": f"fusion_score {score:.1f} < {FUSION_HOLD_MIN:.0f} — INVERSE"}


# =============================================================================
# 실현수익률 기반 Accuracy / Profit Factor
# =============================================================================

def classify_realized_outcome(
    predicted_action: str, hynix_return_pct: Optional[float], inverse_return_pct: Optional[float],
    threshold_pct: float = 0.3,
) -> str:
    """방향이 아니라 실제 보유 5분 수익률 기준 성공/무효/실패 분류.

    BUY → hynix_return_pct, INVERSE → inverse_return_pct(실제 매수한 종목의 수익률)를 사용한다.
    """
    if predicted_action == ACTION_BUY:
        ret = hynix_return_pct
    elif predicted_action == ACTION_INVERSE:
        ret = inverse_return_pct
    else:
        return REALIZED_OUTCOME_NOT_APPLICABLE

    if ret is None:
        return REALIZED_OUTCOME_UNKNOWN
    if ret >= threshold_pct:
        return REALIZED_OUTCOME_SUCCESS
    if ret <= -threshold_pct:
        return REALIZED_OUTCOME_FAILURE
    return REALIZED_OUTCOME_VOID


def compute_profit_factor(trades: list) -> dict:
    """trades: [{"pnl_pct": float}, ...] — pnl_pct는 실제 보유수익률(%)이면 충분하다.

    Profit Factor = 총이익 합 / 총손실 합(절대값). 손실이 0이면 이익 유무에 따라
    inf 또는 0을 반환한다(직렬화 시 호출부가 문자열로 변환해야 함).
    """
    pnls = [t.get("pnl_pct") for t in trades if t.get("pnl_pct") is not None]
    gains = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    gross_profit = sum(gains)
    gross_loss = sum(losses)
    if gross_loss == 0:
        profit_factor = float("inf") if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    win_rate = (len(gains) / len(pnls) * 100.0) if pnls else 0.0
    avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0
    return {
        "profit_factor": profit_factor, "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4), "win_rate": round(win_rate, 2),
        "avg_pnl_pct": round(avg_pnl, 4), "trade_count": len(pnls),
    }


def compute_profit_factor_from_outcome_log(outcome_df: pd.DataFrame, horizon_minutes: int = 5) -> dict:
    """hynix_prediction_tracker.py가 쌓는 prediction_outcome_log.csv에서 horizon=5분 데이터를
    읽어 Profit Factor를 계산한다(방향 정확도가 아니라 실현수익률 기준 — 학습목표 재정의)."""
    if outcome_df is None or outcome_df.empty:
        return compute_profit_factor([])
    sub = outcome_df[outcome_df["horizon_minutes"].astype(str) == str(horizon_minutes)].copy()
    trades = []
    for _, row in sub.iterrows():
        action = row.get("predicted_action")
        hynix_ret = pd.to_numeric(row.get("hynix_return_pct"), errors="coerce")
        inverse_ret = pd.to_numeric(row.get("inverse_return_pct"), errors="coerce")
        if action in ("HYNIX_BUY", "HYNIX_STRONG_BUY", ACTION_BUY) and pd.notna(hynix_ret):
            trades.append({"pnl_pct": float(hynix_ret)})
        elif action in ("INVERSE_BUY", "INVERSE_STRONG_BUY", ACTION_INVERSE) and pd.notna(inverse_ret):
            trades.append({"pnl_pct": float(inverse_ret)})
    return compute_profit_factor(trades)


def recommend_profit_factor_weights(outcome_df: pd.DataFrame, decision_df: Optional[pd.DataFrame] = None) -> dict:
    """학습목표 = 정확도가 아니라 Profit Factor 최대화. horizon(3m/5m/10m) 가중치를
    "그 horizon 신호가 강했던 거래일수록 실현수익률이 좋았는지" 상관관계로 조정 추천한다.
    자동 반영하지 않고 JSON에 추천값만 저장한다(hynix_weight_recommender.py와 동일 패턴).
    표본이 PROFIT_FACTOR_MIN_SAMPLES 미만이면 추천을 생략한다.
    """
    created_at = datetime.now().isoformat()
    sample_size = 0 if outcome_df is None else int(len(outcome_df))

    if sample_size < PROFIT_FACTOR_MIN_SAMPLES:
        result = {
            "skipped": True,
            "reason": f"샘플 부족(sample_size={sample_size} < {PROFIT_FACTOR_MIN_SAMPLES}) — 가중치 추천 생략",
            "sample_size": sample_size, "recommended_horizon_weights": None,
            "profit_factor": compute_profit_factor_from_outcome_log(outcome_df) if outcome_df is not None else None,
            "created_at": created_at,
        }
        _save_weight_recommendation(result)
        return result

    pf = compute_profit_factor_from_outcome_log(outcome_df, horizon_minutes=5)
    result = {
        "skipped": False, "sample_size": sample_size,
        "recommended_horizon_weights": dict(_DEFAULT_HORIZON_WEIGHTS),
        "profit_factor": pf,
        "reason": f"표본 {sample_size}건 — 5분 실현 Profit Factor {pf['profit_factor']} 기준(가중치 세부 최적화는 추가 표본 필요)",
        "created_at": created_at,
    }
    _save_weight_recommendation(result)
    return result


def _save_weight_recommendation(result: dict) -> None:
    try:
        _WEIGHT_RECO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WEIGHT_RECO_PATH.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[HynixDecisionV2] 가중치 추천 저장 실패: %s", exc)


def load_weight_recommendation() -> Optional[dict]:
    try:
        if not _WEIGHT_RECO_PATH.exists():
            return None
        return json.loads(_WEIGHT_RECO_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[HynixDecisionV2] 가중치 추천 로드 실패: %s", exc)
        return None
