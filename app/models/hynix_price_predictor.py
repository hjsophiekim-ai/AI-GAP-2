"""hynix_price_predictor.py — SK하이닉스 다중 horizon(30분/1시간/3시간/오늘종가/내일시가) 가격 예측.

기존 predict_hynix()(오늘/내일/3일/2주 수익률 예측)와는 별개의 병렬 모델이다.
기존 모델/파이프라인은 수정하지 않고, hynix_forecast_engine.run_forecast()에
추가 필드(result["price_prediction"])로만 덧붙인다.

6단계 파이프라인:
  1) 미국 AI/반도체 점수 (MU/NVDA/AMD/AVGO/SOX/QQQ)
  2) 국내 수급/선물/환율 점수 (KOSPI200/USD-KRW/외국인·기관 순매수)
  3) 국내 반도체 섹터 점수 (하이닉스 자체 VWAP 위치 + KOSPI200 대비 상대강도)
  4) SK하이닉스 자체 모멘텀 점수 (RSI/MACD/이평선/거래량 — 일봉 기준)
  5) 가격 예측 엔진 (horizon별 가중합 → 기대수익률 → 현재가 anchor 가격 변환)
  6) 신뢰도/데이터품질 보정 + sanity clip

규칙 기반(rule-based)으로 설계했으며, 추후 ML 모델로 교체 가능하도록
HynixPricePredictor 클래스로 분리했다. 어떤 이유로도 예외를 던지지 않는다
(모든 실패는 결과 dict의 missing_data_warning/message로 표현).

수익 보장 문구는 절대 포함하지 않는다 — 모든 결과는 확률적 추정치다.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

from app.models.hynix_predictor import _resolve_price_anchor, _round_krx

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs" / "hynix_prediction"

MODEL_VERSION = "rule_based_multi_horizon_v1"
HORIZONS = ("30m", "1h", "3h", "close", "tomorrow_open")

# horizon별 4단계 신호 가중치 (각 horizon 합계 = 1.0)
HORIZON_WEIGHTS = {
    "30m":           {"hynix_self": 0.45, "domestic_sector": 0.25, "domestic_flow": 0.15, "us_ai_semi": 0.15},
    "1h":            {"hynix_self": 0.35, "domestic_sector": 0.25, "domestic_flow": 0.20, "us_ai_semi": 0.20},
    "3h":            {"hynix_self": 0.25, "domestic_sector": 0.20, "domestic_flow": 0.20, "us_ai_semi": 0.35},
    "close":         {"hynix_self": 0.20, "domestic_sector": 0.15, "domestic_flow": 0.15, "us_ai_semi": 0.50},
    "tomorrow_open": {"hynix_self": 0.10, "domestic_sector": 0.10, "domestic_flow": 0.15, "us_ai_semi": 0.65},
}
# composite_signal(-1..+1)이 100% 확신일 때 반영할 최대 기대수익률(%)
HORIZON_RETURN_SCALE = {"30m": 0.8, "1h": 1.3, "3h": 2.2, "close": 3.0, "tomorrow_open": 3.5}
# 비현실적 가격(예: "250만원인데 100만원 매수") 방지용 sanity clip 상한(%)
HORIZON_CLIP_PCT = {"30m": 1.5, "1h": 3.0, "3h": 5.0, "close": 7.0, "tomorrow_open": 7.0}
# 방향 확률 계산 시 "횡보"로 간주하는 기대수익률 구간(%)
HORIZON_SIDEWAYS_PCT = {"30m": 0.3, "1h": 0.5, "3h": 0.8, "close": 0.6, "tomorrow_open": 1.0}

_RESULT_KEY = {
    "30m": "30m", "1h": "1h", "3h": "3h",
    "close": "close_today", "tomorrow_open": "open_tomorrow",
}


def _weighted_norm(components: dict) -> tuple:
    """components: {name: (value, scale, weight)} -> (signal(-1..1)|None, used_weight(0..1))."""
    total, weight_sum = 0.0, 0.0
    for _name, (value, scale, weight) in components.items():
        if value is None:
            continue
        norm = max(-1.0, min(1.0, float(value) / scale))
        total += norm * weight
        weight_sum += weight
    signal = total / weight_sum if weight_sum > 1e-9 else None
    return signal, round(weight_sum, 4)


def _mu_effective_return(micron_features: dict) -> Optional[float]:
    for key in ("micron_regular_return", "micron_aftermarket_return", "micron_premarket_return"):
        value = micron_features.get(key)
        if value is not None:
            return float(value)
    return None


class HynixPricePredictor:
    """규칙 기반 다중 horizon 가격 예측기. predict()가 유일한 공개 진입점이다."""

    # ------------------------------------------------------------------
    # Stage 1: 미국 AI/반도체 점수
    # ------------------------------------------------------------------
    def _stage_us_ai_semi(self, market_data: dict, micron_features: dict) -> dict:
        mu = market_data.get("mu", {}) or {}
        nvda = market_data.get("nvda", {}) or {}
        amd = market_data.get("amd", {}) or {}
        avgo = market_data.get("avgo", {}) or {}
        index = market_data.get("index", {}) or {}

        mu_ret = _mu_effective_return(micron_features)
        nvda_ret = nvda.get("regular_return")
        amd_ret = amd.get("regular_return")
        avgo_ret = avgo.get("regular_return")
        sox_ret = index.get("sox_return")
        qqq_ret = index.get("qqq_return")

        mu_relative_strength_vs_sox = None
        if mu_ret is not None and sox_ret is not None:
            mu_relative_strength_vs_sox = round(mu_ret - sox_ret, 3)

        signal, used_weight = _weighted_norm({
            "mu": (mu_ret, 3.0, 0.35),
            "sox": (sox_ret, 2.5, 0.20),
            "nvda": (nvda_ret, 3.5, 0.15),
            "amd": (amd_ret, 4.0, 0.10),
            "avgo": (avgo_ret, 3.0, 0.10),
            "qqq": (qqq_ret, 2.0, 0.10),
        })
        return {
            "signal": signal, "used_weight": used_weight,
            "mu_return": mu_ret, "mu_relative_strength_vs_sox": mu_relative_strength_vs_sox,
            "sox_return": sox_ret, "nvda_return": nvda_ret, "amd_return": amd_ret,
            "avgo_return": avgo_ret, "qqq_return": qqq_ret,
            "sources": {
                "mu": mu.get("source"), "nvda": nvda.get("source"), "amd": amd.get("source"),
                "avgo": avgo.get("source"), "index": index.get("source"),
            },
        }

    # ------------------------------------------------------------------
    # Stage 2: 국내 수급/선물/환율 점수
    # ------------------------------------------------------------------
    def _stage_domestic_flow(self, market_data: dict) -> dict:
        domestic_index = market_data.get("domestic_index", {}) or {}
        index = market_data.get("index", {}) or {}
        investor = market_data.get("investor_flow", {}) or {}

        kospi_ret = domestic_index.get("kospi_return")
        kospi200_ret = domestic_index.get("kospi200_return")
        usdkrw_change = index.get("usdkrw_change")
        foreign_net = investor.get("foreign_net_buy")
        institution_net = investor.get("institution_net_buy")

        flow_values = [v for v in (foreign_net, institution_net) if v is not None]
        flow_signal = None
        if flow_values:
            flow_signal = math.tanh(sum(flow_values) / len(flow_values) / 1_500_000.0)

        futures_proxy_ret = kospi200_ret if kospi200_ret is not None else kospi_ret
        signal, used_weight = _weighted_norm({
            "kospi200_futures_proxy": (futures_proxy_ret, 1.5, 0.35),
            "usdkrw": (-usdkrw_change if usdkrw_change is not None else None, 1.0, 0.25),
            "investor_flow": (flow_signal, 1.0, 0.40),
        })
        return {
            "signal": signal, "used_weight": used_weight,
            "kospi_return": kospi_ret, "kospi200_return": kospi200_ret,
            "kospi200_futures_note": "실제 지수선물 시세 없음 — KOSPI200 현물지수를 근사치로 사용",
            "usdkrw_change": usdkrw_change,
            "foreign_net_buy": foreign_net, "institution_net_buy": institution_net,
            "is_proxy": False,
            "sources": {
                "domestic_index": domestic_index.get("source"), "index": index.get("source"),
                "investor_flow": investor.get("source"),
            },
        }

    # ------------------------------------------------------------------
    # Stage 3: 국내 반도체 섹터 점수 (하이닉스 VWAP + KOSPI200 상대강도로 근사)
    # ------------------------------------------------------------------
    def _stage_domestic_sector(
        self, market_data: dict, hynix_current_price: Optional[float],
        hynix_prev_close: Optional[float], tech_indicators: dict,
    ) -> dict:
        hynix_minute = market_data.get("hynix_minute", {}) or {}
        domestic_index = market_data.get("domestic_index", {}) or {}
        df_1min = hynix_minute.get("df_1min")

        vwap = None
        vwap_position_pct = None
        try:
            if df_1min is not None and not df_1min.empty:
                from app.strategy.intraday_indicators import calculate_vwap

                candles = df_1min.to_dict("records")
                vwap = calculate_vwap(candles)
                if vwap and hynix_current_price:
                    vwap_position_pct = (hynix_current_price - vwap) / vwap * 100
        except Exception as exc:
            logger.debug("[HynixPricePredictor] VWAP 계산 실패: %s", exc)

        kospi200_ret = domestic_index.get("kospi200_return")
        kospi_ret = domestic_index.get("kospi_return")
        benchmark_ret = kospi200_ret if kospi200_ret is not None else kospi_ret

        hynix_today_return_pct = None
        if hynix_current_price and hynix_prev_close:
            hynix_today_return_pct = (hynix_current_price / hynix_prev_close - 1.0) * 100

        relative_strength_vs_kospi200 = None
        if hynix_today_return_pct is not None and benchmark_ret is not None:
            relative_strength_vs_kospi200 = round(hynix_today_return_pct - benchmark_ret, 3)

        ma20_pos = tech_indicators.get("ma20_position_pct")
        from_high = tech_indicators.get("from_20d_high_pct")

        signal, used_weight = _weighted_norm({
            "vwap_position": (vwap_position_pct, 1.0, 0.35),
            "relative_strength": (relative_strength_vs_kospi200, 2.0, 0.35),
            "ma20_position": (ma20_pos, 5.0, 0.15),
            "from_20d_high": (from_high, 10.0, 0.15),
        })
        return {
            "signal": signal, "used_weight": used_weight,
            "vwap": round(vwap, 1) if vwap else None, "vwap_position_pct": round(vwap_position_pct, 3) if vwap_position_pct is not None else None,
            "relative_strength_vs_kospi200": relative_strength_vs_kospi200,
            "note": "삼성전자/한미반도체 개별 실시간 수집 대신, 하이닉스 자체 VWAP 위치 + "
                    "KOSPI200 대비 상대강도를 국내 반도체 섹터 위치의 근사치로 사용함(추가 수집 지연 방지).",
            "sources": {"hynix_minute": hynix_minute.get("source"), "domestic_index": domestic_index.get("source")},
        }

    # ------------------------------------------------------------------
    # Stage 4: SK하이닉스 자체 모멘텀 점수 (일봉 기술적 지표 기준)
    # ------------------------------------------------------------------
    def _stage_hynix_self(self, tech_indicators: dict) -> dict:
        rsi = tech_indicators.get("rsi_14")
        macd_cross = tech_indicators.get("macd_signal_cross")
        ma5_pos = tech_indicators.get("ma5_position_pct")
        return_3d = tech_indicators.get("return_3d_pct")
        vol_change = tech_indicators.get("volume_change_pct")
        bollinger = tech_indicators.get("bollinger_pct")

        rsi_signal = (rsi - 50.0) / 50.0 if rsi is not None else None
        macd_signal = float(macd_cross) if macd_cross is not None else None

        signal, used_weight = _weighted_norm({
            "rsi": (rsi_signal, 1.0, 0.30),
            "macd_cross": (macd_signal, 1.0, 0.20),
            "ma5_position": (ma5_pos, 3.0, 0.25),
            "return_3d": (return_3d, 5.0, 0.15),
            "volume_change": (vol_change, 30.0, 0.10),
        })
        return {
            "signal": signal, "used_weight": used_weight,
            "rsi_14": rsi, "macd_signal_cross": macd_cross, "ma5_position_pct": ma5_pos,
            "return_3d_pct": return_3d, "volume_change_pct": vol_change, "bollinger_pct": bollinger,
            "note": "실시간 5분봉 RSI/MACD 인프라가 없어 일봉 기준 기술적 지표를 사용함.",
        }

    # ------------------------------------------------------------------
    # Stage 5/6: 가격 예측 + 신뢰도/데이터품질 보정
    # ------------------------------------------------------------------
    def predict(
        self,
        market_data: dict,
        hynix_current_price: Optional[float] = None,
        hynix_prev_close: Optional[float] = None,
        tech_indicators: Optional[dict] = None,
        micron_features: Optional[dict] = None,
    ) -> dict:
        tech_indicators = tech_indicators or {}
        micron_features = micron_features or {}
        market_data = market_data or {}

        stage_us = self._stage_us_ai_semi(market_data, micron_features)
        stage_flow = self._stage_domestic_flow(market_data)
        stage_sector = self._stage_domestic_sector(market_data, hynix_current_price, hynix_prev_close, tech_indicators)
        stage_self = self._stage_hynix_self(tech_indicators)
        stage_results = {
            "us_ai_semi": stage_us, "domestic_flow": stage_flow,
            "domestic_sector": stage_sector, "hynix_self": stage_self,
        }

        try:
            from app.market import us_market_data as umd
            us_status = umd.get_us_market_status()
            holiday_mode = bool(us_status.get("is_us_holiday") or us_status.get("is_us_weekend"))
        except Exception as exc:
            logger.debug("[HynixPricePredictor] 미국장 휴장 판단 실패(무시): %s", exc)
            holiday_mode = False

        mu_is_stale = bool(market_data.get("mu", {}).get("is_stale"))
        hynix_source = market_data.get("hynix", {}).get("source")
        investor_source = market_data.get("investor_flow", {}).get("source")

        data_quality_score, missing_warnings = _compute_data_quality(
            stage_results, holiday_mode, mu_is_stale, hynix_source, investor_source,
        )

        base_price, base_source = _resolve_price_anchor(hynix_current_price, hynix_prev_close)
        extreme_event = _is_extreme_event(stage_us)

        result = {
            "model_version": MODEL_VERSION,
            "predicted_at": datetime.now().isoformat(timespec="seconds"),
            "current_price": float(hynix_current_price) if hynix_current_price else None,
            "base_price": base_price if base_price > 0 else None,
            "base_price_source": base_source,
            "holiday_mode": holiday_mode,
            "extreme_event": extreme_event,
            "data_quality_score": data_quality_score,
            "missing_data_warning": missing_warnings,
            "stage_scores": {name: sr.get("signal") for name, sr in stage_results.items()},
            "stage_details": stage_results,
            "key_reasons": _build_key_reasons(stage_results),
            "data_sources_used": {
                "mu": stage_us["sources"].get("mu"), "nvda": stage_us["sources"].get("nvda"),
                "amd": stage_us["sources"].get("amd"), "avgo": stage_us["sources"].get("avgo"),
                "sox_qqq_usdkrw": stage_us["sources"].get("index"),
                "kospi_kospi200": stage_flow["sources"].get("domestic_index"),
                "investor_flow": stage_flow["sources"].get("investor_flow"),
                "hynix_price": hynix_source,
                "hynix_minute_vwap": stage_sector["sources"].get("hynix_minute"),
            },
        }

        if base_price <= 0:
            result["message"] = "가격 기준점(current_price/prev_close)이 없어 다중 horizon 가격 예측을 생성할 수 없습니다."
            for horizon in HORIZONS:
                key = _RESULT_KEY[horizon]
                result[f"predicted_price_{key}" if horizon not in ("close", "tomorrow_open") else (
                    "predicted_close_today" if horizon == "close" else "predicted_open_tomorrow"
                )] = None
            _log_price_prediction(result)
            return result

        for horizon in HORIZONS:
            weights = HORIZON_WEIGHTS[horizon]
            composite, weight_sum = 0.0, 0.0
            for stage_name, w in weights.items():
                sig = stage_results[stage_name].get("signal")
                if sig is None:
                    continue
                composite += sig * w
                weight_sum += w
            composite_signal = composite / weight_sum if weight_sum > 1e-9 else 0.0

            raw_return = composite_signal * HORIZON_RETURN_SCALE[horizon]
            clip = HORIZON_CLIP_PCT[horizon] * (1.4 if extreme_event and horizon in ("close", "tomorrow_open") else 1.0)
            clipped_return = max(-clip, min(clip, raw_return))
            clip_applied = abs(raw_return - clipped_return) > 1e-9

            price = _round_krx(base_price * (1 + clipped_return / 100))
            p_up, p_side, p_down = _direction_probabilities(clipped_return, HORIZON_SIDEWAYS_PCT[horizon])
            horizon_confidence = _horizon_confidence(weights, stage_results, holiday_mode, mu_is_stale, hynix_source)

            price_key = "predicted_close_today" if horizon == "close" else (
                "predicted_open_tomorrow" if horizon == "tomorrow_open" else f"predicted_price_{horizon}"
            )
            suffix = horizon
            result[price_key] = price
            result[f"expected_return_pct_{suffix}"] = round(clipped_return, 3)
            result[f"probability_up_{suffix}"] = p_up
            result[f"probability_sideways_{suffix}"] = p_side
            result[f"probability_down_{suffix}"] = p_down
            result[f"confidence_{suffix}"] = horizon_confidence
            result[f"clip_applied_{suffix}"] = clip_applied

        # 사용자 요구 명세의 별칭 필드(내일 확률은 "_tomorrow"로도 노출)
        result["probability_up_tomorrow"] = result["probability_up_tomorrow_open"]
        result["probability_down_tomorrow"] = result["probability_down_tomorrow_open"]
        result["probability_sideways_tomorrow"] = result["probability_sideways_tomorrow_open"]
        result["confidence_tomorrow_open_alias"] = result["confidence_tomorrow_open"]

        result["message"] = f"데이터 품질 {data_quality_score:.0f}/100 — 다중 horizon 예측 완료"
        _log_price_prediction(result)
        return result


def _is_extreme_event(stage_us: dict) -> bool:
    mu_ret = stage_us.get("mu_return")
    return mu_ret is not None and abs(mu_ret) >= 8.0


def _direction_probabilities(expected_return_pct: Optional[float], sideways_pct: float) -> tuple:
    """확률 3분할. 어떤 경우에도 0%/100%로 수렴하지 않도록 [3, 95] 범위로 캡한다
    (수익 보장/확정처럼 읽히는 결과를 방지하기 위함)."""
    if expected_return_pct is None:
        return 33.4, 33.3, 33.3
    k = 2.5 / max(sideways_pct, 0.05)
    p_up_raw = 1.0 / (1.0 + math.exp(-k * expected_return_pct))
    sideways_peak = math.exp(-((expected_return_pct / sideways_pct) ** 2)) if sideways_pct > 0 else 0.0
    p_side = 0.55 * sideways_peak
    remaining = 1.0 - p_side
    p_up = remaining * p_up_raw
    p_down = remaining * (1.0 - p_up_raw)
    total = p_up + p_side + p_down
    if total <= 0:
        return 33.4, 33.3, 33.3
    p_up, p_side, p_down = (p_up / total * 100, p_side / total * 100, p_down / total * 100)
    p_up = max(3.0, min(95.0, p_up))
    p_down = max(3.0, min(95.0, p_down))
    p_side = max(2.0, 100.0 - p_up - p_down)
    total2 = p_up + p_side + p_down
    return round(p_up / total2 * 100, 1), round(p_side / total2 * 100, 1), round(p_down / total2 * 100, 1)


def _horizon_confidence(weights: dict, stage_results: dict, holiday_mode: bool, mu_is_stale: bool, hynix_source) -> float:
    score = 0.0
    for stage_name, w in weights.items():
        completeness = stage_results[stage_name].get("used_weight") or 0.0
        score += w * completeness
    conf = score * 100.0
    if mu_is_stale:
        conf *= 0.90
    if hynix_source not in ("KIS", "kis"):
        conf *= 0.95
    if holiday_mode:
        conf = min(conf, 85.0)
    return round(max(0.0, min(100.0, conf)), 1)


def _compute_data_quality(stage_results: dict, holiday_mode: bool, mu_is_stale: bool, hynix_source, investor_source) -> tuple:
    warnings: list[str] = []
    completeness = [sr.get("used_weight") or 0.0 for sr in stage_results.values()]
    base = sum(completeness) / len(completeness) * 100.0 if completeness else 0.0

    if stage_results["us_ai_semi"].get("used_weight", 0.0) < 0.5:
        warnings.append("미국 반도체(MU/NVDA/AMD/AVGO/SOX) 데이터 다수 누락 — 예측 신뢰도가 낮습니다")
    if stage_results["domestic_flow"].get("used_weight", 0.0) < 0.5:
        warnings.append("KOSPI200/환율/수급 데이터가 다수 누락되었습니다")
    if stage_results["domestic_sector"].get("used_weight", 0.0) < 0.5:
        warnings.append("하이닉스 VWAP/상대강도 데이터가 부족합니다")
    if stage_results["hynix_self"].get("used_weight", 0.0) < 0.5:
        warnings.append("하이닉스 자체 기술적 지표(RSI/MACD 등)가 부족합니다")
    if mu_is_stale:
        base *= 0.90
        warnings.append("MU(마이크론) 데이터가 15분 이상 지연(stale) 상태입니다")
    if hynix_source not in ("KIS", "kis"):
        base *= 0.95
        warnings.append(f"SK하이닉스 시세 소스가 KIS가 아닌 '{hynix_source}'을(를) 사용 중입니다")
    if investor_source == "cache":
        base *= 0.90
        warnings.append("외국인/기관 수급 데이터가 캐시(지연) 값입니다")
    if holiday_mode:
        base = min(base, 85.0)
        warnings.append("미국 시장 휴장/주말 — 데이터 신선도 제약으로 신뢰도 상한 85점이 적용됩니다")

    return round(max(0.0, min(100.0, base)), 1), warnings


def _build_key_reasons(stage_results: dict) -> list:
    candidates: list[tuple] = []
    us = stage_results["us_ai_semi"]
    if us.get("mu_return") is not None:
        candidates.append((abs(us["mu_return"]), f"미국 반도체 MU {us['mu_return']:+.2f}% ({us['sources'].get('mu')})"))
    if us.get("mu_relative_strength_vs_sox") is not None:
        v = us["mu_relative_strength_vs_sox"]
        candidates.append((abs(v) * 0.8, f"MU vs SOX 상대강도 {v:+.2f}%p"))
    if us.get("sox_return") is not None:
        candidates.append((abs(us["sox_return"]), f"필라델피아 반도체지수(SOX) {us['sox_return']:+.2f}%"))
    if us.get("nvda_return") is not None:
        candidates.append((abs(us["nvda_return"]) * 0.6, f"NVDA {us['nvda_return']:+.2f}%"))
    flow = stage_results["domestic_flow"]
    if flow.get("usdkrw_change") is not None:
        candidates.append((abs(flow["usdkrw_change"]) * 0.8, f"원/달러 환율 변화율 {flow['usdkrw_change']:+.2f}%"))
    if flow.get("foreign_net_buy") is not None:
        candidates.append((abs(flow["foreign_net_buy"]) / 50_000.0, f"외국인 순매수 {flow['foreign_net_buy']:+,.0f}주"))
    if flow.get("kospi200_return") is not None:
        candidates.append((abs(flow["kospi200_return"]) * 0.7, f"KOSPI200 등락률 {flow['kospi200_return']:+.2f}%"))
    sector = stage_results["domestic_sector"]
    if sector.get("vwap_position_pct") is not None:
        candidates.append((abs(sector["vwap_position_pct"]) * 0.9, f"SK하이닉스 VWAP 대비 {sector['vwap_position_pct']:+.2f}%"))
    if sector.get("relative_strength_vs_kospi200") is not None:
        v = sector["relative_strength_vs_kospi200"]
        candidates.append((abs(v) * 0.7, f"KOSPI200 대비 상대강도 {v:+.2f}%p"))
    selfd = stage_results["hynix_self"]
    if selfd.get("rsi_14") is not None:
        candidates.append((abs(selfd["rsi_14"] - 50) * 0.5, f"RSI(14) {selfd['rsi_14']:.1f}"))
    if selfd.get("return_3d_pct") is not None:
        candidates.append((abs(selfd["return_3d_pct"]) * 0.5, f"최근 3일 수익률 {selfd['return_3d_pct']:+.2f}%"))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in candidates[:5]]


def _log_price_prediction(result: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "logged_at": datetime.now().isoformat(timespec="seconds"),
            "predicted_at": result.get("predicted_at"),
            "current_price": result.get("current_price"),
            "base_price": result.get("base_price"),
            "base_price_source": result.get("base_price_source"),
            "predicted_price_30m": result.get("predicted_price_30m"),
            "predicted_price_1h": result.get("predicted_price_1h"),
            "predicted_price_3h": result.get("predicted_price_3h"),
            "predicted_close_today": result.get("predicted_close_today"),
            "predicted_open_tomorrow": result.get("predicted_open_tomorrow"),
            "confidence_30m": result.get("confidence_30m"),
            "confidence_1h": result.get("confidence_1h"),
            "confidence_3h": result.get("confidence_3h"),
            "confidence_close": result.get("confidence_close"),
            "confidence_tomorrow_open": result.get("confidence_tomorrow_open"),
            "data_quality_score": result.get("data_quality_score"),
            "holiday_mode": result.get("holiday_mode"),
            "model_version": result.get("model_version"),
            "actual_price_30m": None, "actual_price_1h": None, "actual_price_3h": None,
            "actual_close_today": None, "actual_open_tomorrow": None,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("[HynixPricePredictor] 예측 로그 기록 실패(무해): %s", exc)


def predict_hynix_multi_horizon(
    market_data: dict,
    hynix_current_price: Optional[float] = None,
    hynix_prev_close: Optional[float] = None,
    tech_indicators: Optional[dict] = None,
    micron_features: Optional[dict] = None,
) -> dict:
    """모듈 수준 진입점 — HynixPricePredictor().predict()의 얇은 래퍼."""
    return HynixPricePredictor().predict(
        market_data=market_data,
        hynix_current_price=hynix_current_price,
        hynix_prev_close=hynix_prev_close,
        tech_indicators=tech_indicators,
        micron_features=micron_features,
    )
