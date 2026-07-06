"""
regime_router.py

Market Regime Router 메인 오케스트레이터.

흐름: market_data_collector 수집 -> (09:20 기준 저점/지수 스냅샷 캐시) ->
regime_features 점수화 -> regime_rules 로 A~F + confidence_score 확정 ->
logs/market_regime/YYYYMMDD.json 저장.

08:50~09:20: 관찰 구간 (임시 판단만, 확정 아님)
09:20 이후: 최종 유형 확정 (0920 저점/지수 스냅샷을 최초 1회 캐시)
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market.market_data_collector import MarketDataCollector
from app.market import regime_features as rf
from app.market.regime_rules import decide_regime, RegimeDecision
from app.market import market_prediction as mp
from app.market import market_alert as ma
from app.market import tick_history

_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_DIR = _ROOT / "data" / "state"
_LOG_DIR = _ROOT / "logs" / "market_regime"
_PREDICTION_LOG_DIR = _ROOT / "logs" / "market_prediction"
_FEATURE_SNAPSHOT_LOG_DIR = _ROOT / "logs" / "feature_snapshots"

CONFIRM_TIME = "09:20"
REEVALUATION_INTERVAL_MINUTES = 5
REEVALUATION_END_TIME = "11:10"


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _ref_snapshot_path(date_str: str) -> Path:
    return _STATE_DIR / f"regime_0920_ref_{date_str}.json"


def load_0920_reference(date_str: str = None) -> Optional[dict]:
    date_str = date_str or _today()
    path = _ref_snapshot_path(date_str)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.debug("[RegimeRouter] 0920 기준 스냅샷 로드 실패: %s", exc)
        return None


def save_0920_reference(snapshot: dict, date_str: str = None) -> dict:
    date_str = date_str or _today()
    domestic = snapshot.get("domestic", {})
    ref = {
        "kospi_value": domestic.get("kospi", {}).get("value"),
        "kosdaq_value": domestic.get("kosdaq", {}).get("value"),
        "hynix_low": domestic.get("hynix", {}).get("low") or domestic.get("hynix", {}).get("current_price"),
        "samsung_low": domestic.get("samsung", {}).get("low") or domestic.get("samsung", {}).get("current_price"),
        "leader_sectors_0920": rf._leader_sectors(snapshot),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ref_snapshot_path(date_str), "w", encoding="utf-8") as f:
            json.dump(ref, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("[RegimeRouter] 0920 기준 스냅샷 저장 실패: %s", exc)
    return ref


def _get_or_create_0920_reference(snapshot: dict, now_hm: str, date_str: str) -> Optional[dict]:
    """09:20 이후 최초 호출 시 그 시점 저점/지수를 기준으로 캐시한다."""
    if now_hm < CONFIRM_TIME:
        return None
    existing = load_0920_reference(date_str)
    if existing:
        return existing
    return save_0920_reference(snapshot, date_str)


# ---------------------------------------------------------------------------
# 동적 재판단 상태 (initial_regime / current_regime / regime_history)
# ---------------------------------------------------------------------------

def _history_path(date_str: str) -> Path:
    return _STATE_DIR / f"regime_history_{date_str}.json"


def load_regime_history(date_str: str = None) -> dict:
    date_str = date_str or _today()
    path = _history_path(date_str)
    if not path.exists():
        return {"initial_regime": None, "initial_confidence": None, "initial_at": None, "history": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.debug("[RegimeRouter] regime_history 로드 실패: %s", exc)
        return {"initial_regime": None, "initial_confidence": None, "initial_at": None, "history": []}


def _save_regime_history(state: dict, date_str: str) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_history_path(date_str), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.warning("[RegimeRouter] regime_history 저장 실패: %s", exc)


def _update_regime_history(decision: RegimeDecision, is_confirmed: bool, now_hm: str, date_str: str) -> dict:
    """09:20 최초 확정 시 initial_regime을 고정하고, 매 호출마다 history에 기록한다."""
    state = load_regime_history(date_str)
    if is_confirmed and state.get("initial_regime") is None:
        state["initial_regime"] = decision.regime
        state["initial_confidence"] = decision.confidence_score
        state["initial_at"] = datetime.now().isoformat(timespec="seconds")

    state.setdefault("history", []).append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "now_hm": now_hm,
        "regime": decision.regime,
        "confidence": decision.confidence_score,
        "policy_name": decision.policy_name,
    })
    _save_regime_history(state, date_str)
    return state


def _regime_change_risk(initial_regime: Optional[str], current_regime: str) -> float:
    """initial_regime 대비 current_regime 변화 위험도(0~100)."""
    if not initial_regime or initial_regime == current_regime:
        return 0.0
    bad_regimes = {"D", "E"}
    if current_regime in bad_regimes and initial_regime not in bad_regimes:
        return 80.0
    if current_regime == "F" and initial_regime not in bad_regimes:
        return 40.0
    if current_regime not in bad_regimes and initial_regime in bad_regimes:
        return 20.0  # 악화에서 회복 방향 전환 — 위험도는 낮음
    return 50.0


def should_reevaluate(last_run_iso: Optional[str], now: datetime = None, interval_minutes: int = REEVALUATION_INTERVAL_MINUTES) -> bool:
    """마지막 실행 이후 interval_minutes가 지났는지 확인한다 (UI/스케줄러가 사용)."""
    if not last_run_iso:
        return True
    now = now or datetime.now()
    try:
        last_run = datetime.fromisoformat(last_run_iso)
    except Exception:
        return True
    return (now - last_run).total_seconds() >= interval_minutes * 60


def _regime_transition_momentum(score_deltas: dict) -> Optional[float]:
    """양수=완화(회복) 방향 모멘텀, 음수=악화 방향 모멘텀. 델타 데이터가 전혀 없으면 None."""
    parts = []
    mc15 = score_deltas.get("market_collapse_score_delta_15m")
    if mc15 is not None:
        parts.append(-mc15)
    sc15 = score_deltas.get("semiconductor_collapse_score_delta_15m")
    if sc15 is not None:
        parts.append(-sc15)
    rec15 = score_deltas.get("recovery_score_delta_15m")
    if rec15 is not None:
        parts.append(rec15)
    if not parts:
        return None
    return round(sum(parts) / len(parts), 2)


def _compute_score_deltas(scores: dict, recovery_info: dict, date_str: str) -> dict:
    """
    market_collapse_score/semiconductor_collapse_score/risk_off_score/recovery_score의
    5분/15분 변화량을 계산한다. "절대값이 아니라 변화 방향/추세를 본다"는 원칙을
    구현하기 위한 전용 시계열(logs와 별개, data/state/market_ticks/score_ticks_*.jsonl)이다.
    """
    score_ticks_before = tick_history.load_score_ticks(date_str)
    current_tick = tick_history.append_score_tick({
        "market_collapse_score": scores.get("market_collapse_score"),
        "semiconductor_collapse_score": scores.get("semiconductor_collapse_score"),
        "risk_off_score": scores.get("risk_off_score"),
        "recovery_score": recovery_info.get("recovery_score"),
    }, date_str)

    deltas = {}
    for field in ("market_collapse_score", "semiconductor_collapse_score", "risk_off_score", "recovery_score"):
        for minutes, suffix in ((5, "5m"), (15, "15m")):
            if field == "risk_off_score" and suffix == "15m":
                continue  # 명세상 risk_off_score는 5분 델타만 사용
            deltas[f"{field}_delta_{suffix}"] = tick_history.compute_delta(
                current_tick.get(field), score_ticks_before, field, minutes,
            )
    deltas["regime_transition_momentum"] = _regime_transition_momentum(deltas)
    return deltas


def _save_prediction_log(entry: dict, date_str: str) -> None:
    try:
        _PREDICTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _PREDICTION_LOG_DIR / f"{date_str}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("[RegimeRouter] market_prediction 로그 저장 실패: %s", exc)


class MarketRegimeRouter:
    def __init__(self, cfg=None, market_cfg: dict = None, collector: MarketDataCollector = None):
        self.cfg = cfg
        self.market_cfg = market_cfg or {}
        self.collector = collector or MarketDataCollector(cfg=cfg)

    def determine_regime(self, now_hm: str = None, snapshot: dict = None) -> dict:
        """시장 유형을 판단하고 결과 dict를 반환한다 (RegimeDecision + snapshot 포함)."""
        now_hm = now_hm or _now_hm()
        date_str = _today()

        if snapshot is None:
            snapshot = self.collector.collect()

        ref_0920 = _get_or_create_0920_reference(snapshot, now_hm, date_str)

        scores = rf.compute_all_scores(snapshot, ref_0920)
        scores.update(rf.compute_prediction_scores(snapshot, ref_0920))
        flags = rf.compute_flags(snapshot, ref_0920)
        recovery_info = rf.compute_recovery_score(snapshot, ref_0920)

        holiday_mode = rf.is_holiday_mode(snapshot)
        data_gap_reason = rf.classify_data_gap_reason(snapshot)
        data_freshness_score = rf.compute_data_freshness_score(snapshot)
        data_quality_score = rf.compute_data_quality_score(snapshot)
        if holiday_mode:
            scores["us_ai_score_holiday_adjusted"] = rf.compute_holiday_adjusted_us_score(snapshot)

        # 데이터 품질을 confidence_score 산식에 반영한다. 휴장으로 인한 공백은
        # 과도하게 낮추지 않고(0.92~1.0), 일반 개장일 API 실패는 크게 감점한다(0.70~1.0).
        if data_gap_reason == "API_FAILURE":
            quality_factor = 0.70 + 0.30 * (data_quality_score / 100)
        elif data_gap_reason in ("US_HOLIDAY", "WEEKEND", "EARLY_CLOSE"):
            quality_factor = 0.92 + 0.08 * (data_quality_score / 100)
        else:
            quality_factor = 0.85 + 0.15 * (data_quality_score / 100)

        score_keys = [
            "us_ai_score", "korea_open_score", "leader_sector_score",
            "semiconductor_rebound_score", "risk_off_score", "gap_failure_score",
            "us_ai_score_holiday_adjusted",
        ]
        scaled_scores = dict(scores)
        for k in score_keys:
            if k in scaled_scores:
                scaled_scores[k] = round(scaled_scores[k] * quality_factor, 2)

        confidence_threshold = self.market_cfg.get("confidence_threshold", 60)
        decision: RegimeDecision = decide_regime(
            scaled_scores, flags, cfg={"confidence_threshold": confidence_threshold},
            holiday_mode=holiday_mode,
        )

        if holiday_mode:
            decision.reasons.append(
                f"Holiday Mode 적용(사유={data_gap_reason}, 품질계수={quality_factor:.2f}) "
                "— 국내 09:20 흐름/미국 마지막거래일/선물/환율 대체 판단"
            )

        is_confirmed = now_hm >= CONFIRM_TIME
        data_quality_ratio = snapshot.get("meta", {}).get("data_quality_ratio", 1.0)
        if data_quality_ratio < 0.5:
            decision.reasons.append(f"데이터 품질 저하({data_quality_ratio:.0%}) — 신뢰도 참고용")

        us_status = snapshot.get("overseas", {}).get("us_market_status", {})
        mu_realtime = snapshot.get("overseas", {}).get("us_realtime_bars", {}).get("micron", {})
        mu_last_session = snapshot.get("overseas", {}).get("us_last_session", {}).get("micron", {})
        if mu_realtime.get("success") and mu_realtime.get("data_gap_reason") == "NORMAL":
            mu_data_status, mu_data_source = "REALTIME", mu_realtime.get("source", "unknown")
        elif mu_realtime.get("success"):
            mu_data_status, mu_data_source = "DELAYED", mu_realtime.get("source", "unknown")
        elif mu_last_session.get("success"):
            mu_data_status, mu_data_source = "LAST_SESSION", mu_last_session.get("source", "unknown")
        else:
            mu_data_status, mu_data_source = "MISSING", "none"

        result = {
            "regime": decision.regime,
            "regime_label": decision.label(),
            "confidence_score": decision.confidence_score,
            "reasons": decision.reasons,
            "policy_name": decision.policy_name,
            "is_confirmed": is_confirmed,
            "confirmed_at": CONFIRM_TIME,
            "scores": scores,
            "flags": flags,
            "all_candidate_scores": {k: v[0] for k, v in decision.all_candidate_scores.items()},
            "data_quality_ratio": data_quality_ratio,
            "ref_0920": ref_0920,
            "determined_at": datetime.now().isoformat(timespec="seconds"),
            "date": date_str,
            "holiday_mode": holiday_mode,
            "data_gap_reason": data_gap_reason,
            "data_freshness_score": data_freshness_score,
            "data_quality_score": data_quality_score,
            "us_market_status": us_status,
            "mu_data_status": mu_data_status,
            "mu_data_source": mu_data_source,
        }

        # ── 동적 재판단: initial/current regime, regime_history, regime_change_risk ──
        regime_history_state = _update_regime_history(decision, is_confirmed, now_hm, date_str)
        initial_regime = regime_history_state.get("initial_regime") or decision.regime
        current_regime = decision.regime
        regime_change_risk = _regime_change_risk(initial_regime, current_regime)

        # ── 위험/회복 점수의 5분/15분 변화(관성 편향 완화용) ────────────────────
        score_deltas = _compute_score_deltas(scores, recovery_info, date_str)
        self._append_feature_snapshot(result_partial={
            "current_regime": current_regime, "scores": scores, "recovery_info": recovery_info,
            "snapshot": snapshot, "data_quality_score": data_quality_score,
        }, date_str=date_str)

        # ── 향후 30분/1시간/3시간 + 내일장 예측 ──────────────────────────────
        predictions = mp.predict_all_horizons(snapshot, result, ref_0920, recovery_info, score_deltas)
        tomorrow_prediction = mp.predict_tomorrow_market(
            snapshot, regime_history=regime_history_state.get("history"), now_hm=now_hm, ref_0920=ref_0920,
        )

        # ── 조기경보 ──────────────────────────────────────────────────────────
        alert = ma.compute_alert_from_results(result, predictions["30m"], snapshot)

        kospi_rate = (snapshot.get("domestic", {}).get("kospi", {}) or {}).get("change_rate") or 0.0
        avg_up = round(sum(predictions[h]["probability_up"] for h in mp.HORIZONS) / len(mp.HORIZONS), 1)
        avg_down = round(sum(predictions[h]["probability_down"] for h in mp.HORIZONS) / len(mp.HORIZONS), 1)
        if kospi_rate >= 0:
            trend_continuation_probability, recovery_probability = avg_up, avg_down
        else:
            trend_continuation_probability, recovery_probability = avg_down, avg_up

        result.update({
            "initial_regime": initial_regime,
            "current_regime": current_regime,
            "predicted_regime_30m": predictions["30m"]["expected_regime"],
            "predicted_regime_1h": predictions["1h"]["expected_regime"],
            "predicted_regime_3h": predictions["3h"]["expected_regime"],
            "predicted_regime_tomorrow": tomorrow_prediction["tomorrow_direction"],
            "regime_change_risk": regime_change_risk,
            "market_collapse_score": scores.get("market_collapse_score"),
            "semiconductor_collapse_score": scores.get("semiconductor_collapse_score"),
            "recovery_probability": recovery_probability,
            "trend_continuation_probability": trend_continuation_probability,
            "recovery_score": recovery_info.get("recovery_score"),
            "recovery_score_components": recovery_info.get("components"),
            "recovery_score_unavailable": recovery_info.get("unavailable"),
            "score_deltas": score_deltas,
            "predictions": predictions,
            "tomorrow_prediction": tomorrow_prediction,
            "alert_level": alert.alert_level,
            "alert_reasons": alert.reasons,
            "action_recommendation": alert.action_recommendation,
        })

        self._save_log(result, snapshot, date_str)
        self._save_prediction_entry(result, date_str)
        logger.info(
            "[RegimeRouter] 유형=%s(%s) 신뢰도=%.1f 정책=%s confirmed=%s alert=%s",
            decision.regime, decision.label(), decision.confidence_score,
            decision.policy_name, is_confirmed, alert.alert_level,
        )
        # 로그 저장 후에 원본 snapshot을 결과에 포함한다(정책 모듈이 재사용).
        # _save_log 호출 뒤에 추가해야 logs/market_regime/*.json 이 원본 수집
        # 데이터로 중복 비대해지는 것을 막을 수 있다.
        result["snapshot"] = snapshot
        return result

    def _save_prediction_entry(self, result: dict, date_str: str) -> None:
        """logs/market_prediction/YYYYMMDD.jsonl 에 5분 재평가 시계열 한 줄을 남긴다.

        confidence_score/probability_up/down/sideways(horizon별)와 data_quality_score,
        recovery_score를 함께 남겨야 이후 백테스트에서 신뢰도/데이터품질 구간별 분석이
        가능하다(과거 백테스트 리포트에서 확인된 로그 스키마 공백 보완).
        """
        scores = result.get("scores", {})
        predictions = result.get("predictions", {}) or {}
        entry = {
            "timestamp": result.get("determined_at"),
            "initial_regime": result.get("initial_regime"),
            "current_regime": result.get("current_regime"),
            "predicted_regime_30m": result.get("predicted_regime_30m"),
            "predicted_regime_1h": result.get("predicted_regime_1h"),
            "predicted_regime_3h": result.get("predicted_regime_3h"),
            "tomorrow_prediction": result.get("tomorrow_prediction"),
            "market_collapse_score": result.get("market_collapse_score"),
            "semiconductor_collapse_score": result.get("semiconductor_collapse_score"),
            "foreign_flow_reversal_score": scores.get("foreign_flow_reversal_score"),
            "futures_pressure_score": scores.get("futures_pressure_score"),
            "fx_risk_score": scores.get("fx_risk_score"),
            "breadth_deterioration_score": scores.get("breadth_deterioration_score"),
            "theme_rotation_score": scores.get("theme_rotation_score"),
            "recovery_score": result.get("recovery_score"),
            "score_deltas": result.get("score_deltas"),
            "data_quality_score": result.get("data_quality_score"),
            "holiday_mode": result.get("holiday_mode"),
            "key_reasons": predictions.get("30m", {}).get("key_reasons"),
            "alert_level": result.get("alert_level"),
            "action_recommendation": result.get("action_recommendation"),
        }
        for horizon in ("30m", "1h", "3h"):
            p = predictions.get(horizon, {})
            entry[f"confidence_{horizon}"] = p.get("confidence_score")
            entry[f"probability_up_{horizon}"] = p.get("probability_up")
            entry[f"probability_down_{horizon}"] = p.get("probability_down")
            entry[f"probability_sideways_{horizon}"] = p.get("probability_sideways")
        _save_prediction_log(entry, date_str)

    def _append_feature_snapshot(self, result_partial: dict, date_str: str) -> None:
        """logs/feature_snapshots/YYYYMMDD.jsonl 에 매 tick 핵심 feature를 남긴다.

        market_prediction 로그(예측 결과)와 달리, 이 로그는 "그 순간의 원시
        feature 값"을 남겨 이후 5분/15분/30분/60분 등 임의 구간 회고 분석에
        쓴다(예측 정확도와 무관하게 feature 자체의 변화를 추적하기 위함).
        """
        try:
            snapshot = result_partial["snapshot"]
            scores = result_partial["scores"]
            recovery_info = result_partial["recovery_info"]
            domestic = snapshot.get("domestic", {})
            overseas = snapshot.get("overseas", {})
            hynix = domestic.get("hynix", {}) or {}
            samsung = domestic.get("samsung", {}) or {}

            hynix_price = hynix.get("current_price")
            hynix_vwap = hynix.get("vwap")
            if hynix_price and hynix_vwap:
                hynix_vwap_state = "ABOVE" if hynix_price >= hynix_vwap else "BELOW"
            else:
                hynix_vwap_state = "UNKNOWN"

            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "current_regime": result_partial.get("current_regime"),
                "market_collapse_score": scores.get("market_collapse_score"),
                "semiconductor_collapse_score": scores.get("semiconductor_collapse_score"),
                "recovery_score": recovery_info.get("recovery_score"),
                "futures_pressure_score": scores.get("futures_pressure_score"),
                "foreign_flow_reversal_score": scores.get("foreign_flow_reversal_score"),
                "fx_risk_score": scores.get("fx_risk_score"),
                "breadth_score": scores.get("breadth_deterioration_score"),
                "hynix_vwap_state": hynix_vwap_state,
                "hynix_price": hynix_price,
                "samsung_price": samsung.get("current_price"),
                "kospi200_futures": (domestic.get("kospi200_futures") or {}).get("value"),
                "usdkrw": (overseas.get("usdkrw") or {}).get("value"),
                "data_quality_score": result_partial.get("data_quality_score"),
            }
            _FEATURE_SNAPSHOT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = _FEATURE_SNAPSHOT_LOG_DIR / f"{date_str}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.debug("[RegimeRouter] feature_snapshot 로그 저장 실패(무해): %s", exc)

    def _save_log(self, result: dict, snapshot: dict, date_str: str) -> None:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = _LOG_DIR / f"{date_str}.json"
            existing_entries = []
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing_entries = json.load(f)
                    if not isinstance(existing_entries, list):
                        existing_entries = [existing_entries]
                except Exception:
                    existing_entries = []
            entry = {
                "result": result,
                "collected_data_meta": snapshot.get("meta", {}),
            }
            existing_entries.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing_entries, f, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            logger.warning("[RegimeRouter] 로그 저장 실패: %s", exc)


def determine_regime(cfg=None, market_cfg: dict = None, now_hm: str = None, snapshot: dict = None) -> dict:
    """모듈 수준 편의 함수."""
    router = MarketRegimeRouter(cfg=cfg, market_cfg=market_cfg)
    return router.determine_regime(now_hm=now_hm, snapshot=snapshot)
