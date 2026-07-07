"""hynix_price_predictor.py — SK하이닉스 다중 horizon(30분/1시간/3시간/오늘종가/내일시가) 가격 예측.

기존 predict_hynix()(오늘/내일/3일/2주 수익률 예측)와는 별개의 병렬 모델이다.
기존 모델/파이프라인은 수정하지 않고, hynix_forecast_engine.run_forecast()에
추가 필드(result["price_prediction"])로만 덧붙인다.

6단계 파이프라인:
  1) 미국 AI/반도체 점수 (MU/NVDA/AMD/AVGO/SOX/QQQ, MU 상대강도 포함)
  2) 국내 수급/선물/환율 점수 (KOSPI200/USD-KRW/외국인·기관 순매수)
  3) 국내 반도체 섹터 점수 (하이닉스 자체 VWAP 위치 + KOSPI200 대비 상대강도)
  4) SK하이닉스 자체 모멘텀 점수 (RSI/MACD/이평선/거래량 — 일봉 기준)
  4.5) 회복(recovery) 점수 — "위험은 계속된다"는 관성 편향을 줄이기 위한 반등 신호
  5) 가격 예측 엔진 (horizon별 가중합 → 기대수익률 → 현재가 anchor 가격 변환)
  6) 신뢰도/데이터품질 보정 + sanity clip + rolling bias 보정

규칙 기반(rule-based)으로 설계했으며, 추후 ML 모델로 교체 가능하도록
HynixPricePredictor 클래스로 분리했다. 어떤 이유로도 예외를 던지지 않는다
(모든 실패는 결과 dict의 missing_data_warning/message로 표현).

수익 보장/확정 예측 문구는 절대 포함하지 않는다 — 모든 결과는 확률적 추정치다.
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
from app.models import model_calibration

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs" / "hynix_prediction"

MODEL_VERSION = "rule_based_multi_horizon_v2_recovery_aware"
HORIZONS = ("30m", "1h", "3h", "close", "tomorrow_open")

# horizon별 4단계 신호 가중치 (각 horizon 합계 = 1.0). tomorrow_open은 별도
# 전용 산식(_compute_tomorrow_open_signal)을 쓰므로 여기 값은 참고용 폴백이다.
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
HORIZON_SIDEWAYS_PCT = {"30m": 0.3, "1h": 0.5, "3h": 0.8, "close": 1.0, "tomorrow_open": 1.2}
# "위험은 계속된다"는 관성 편향을 줄이기 위해 recovery_score를 반영하는 비중(horizon별).
# 3시간/종가/내일시가일수록 회복 신호를 더 크게 반영한다.
HORIZON_RECOVERY_INFLUENCE = {"30m": 0.05, "1h": 0.15, "3h": 0.30, "close": 0.35, "tomorrow_open": 0.30}
# market_collapse_score/semiconductor_collapse_score가 70을 넘는 극단적 급락 구간에서
# 추가로 반영하는 하락 페널티의 horizon별 최대치(%)
HORIZON_COLLAPSE_PENALTY_SCALE = {"30m": 0.3, "1h": 0.5, "3h": 0.8, "close": 1.0, "tomorrow_open": 1.0}
HORIZON_RELIABILITY_CAP = {"30m": 85.0, "1h": 78.0, "3h": 68.0, "close": 60.0, "tomorrow_open": 55.0}

_RESULT_KEY = {"30m": "30m", "1h": "1h", "3h": "3h", "close": "close_today", "tomorrow_open": "open_tomorrow"}

# MU 장외(프리마켓/애프터마켓) 데이터를 하이닉스 예측에 얼마나 강하게 반영할지.
# 내일 시가는 MU 장외 score가 핵심 입력값이어야 하므로 25~35% 비중을 명시적으로 배정한다.
MU_EXTENDED_HOURS_TOMORROW_WEIGHT = 0.30
# 08:30~09:00 KST(한국장 개장 직전)에는 MU 장외 score를 사실상 핵심값으로 격상한다.
MU_EXTENDED_HOURS_PREOPEN_WEIGHT = 0.50
MU_EXTENDED_HOURS_PREOPEN_WINDOW = ("08:30", "09:00")
# 장중(30m/1h/3h/close) horizon에서 MU 장외 신호를 반영하는 비중(관성 편향 완화와 동일한
# 축을 공유 — 3시간/종가일수록 크게 반영).
MU_EXTENDED_HOURS_INFLUENCE = {"30m": 0.05, "1h": 0.10, "3h": 0.20, "close": 0.20, "tomorrow_open": 0.0}
MU_LOG_DIR = ROOT / "logs" / "mu_extended_hours"


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


def _norm01(value: Optional[float], scale: float) -> Optional[float]:
    """value(-scale..+scale) -> 0~100 점수(0=strong negative, 50=neutral, 100=strong positive)."""
    if value is None:
        return None
    return max(0.0, min(100.0, 50.0 + (value / scale) * 50.0))


def _mu_effective_return(micron_features: dict) -> Optional[float]:
    for key in ("micron_regular_return", "micron_aftermarket_return", "micron_premarket_return"):
        value = micron_features.get(key)
        if value is not None:
            return float(value)
    return None


def _mu_data_status(mu: dict) -> str:
    if not mu.get("current_price"):
        return "MISSING"
    gap = mu.get("data_gap_reason", "NORMAL")
    if gap in ("US_HOLIDAY", "WEEKEND", "EARLY_CLOSE_OR_CLOSED"):
        return "LAST_SESSION"
    if mu.get("is_stale"):
        return "DELAYED"
    return "REALTIME"


def _tomorrow_state(now_hm: Optional[str] = None) -> str:
    """
    INTRADAY_PRELIMINARY(장중, 단정 금지) / CLOSING_BASED(장마감 후) /
    US_SESSION_UPDATED(다음날 08:50 이전, 미국장 실결과 반영 중) /
    PREOPEN_FINAL(다음날 08:50~09:00, 한국장 개장 직전 최종판단).
    """
    now_hm = now_hm or datetime.now().strftime("%H:%M")
    if now_hm < "08:50":
        return "US_SESSION_UPDATED"
    if now_hm < "09:00":
        return "PREOPEN_FINAL"
    if now_hm < "15:30":
        return "INTRADAY_PRELIMINARY"
    return "CLOSING_BASED"


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

        mu_relative_strength_vs_sox = round(mu_ret - sox_ret, 3) if (mu_ret is not None and sox_ret is not None) else None
        mu_relative_strength_vs_qqq = round(mu_ret - qqq_ret, 3) if (mu_ret is not None and qqq_ret is not None) else None

        signal, used_weight = _weighted_norm({
            "mu": (mu_ret, 3.0, 0.35),
            "sox": (sox_ret, 2.5, 0.20),
            "nvda": (nvda_ret, 3.5, 0.15),
            "amd": (amd_ret, 4.0, 0.10),
            "avgo": (avgo_ret, 3.0, 0.10),
            "qqq": (qqq_ret, 2.0, 0.10),
        })
        mu_ext = mu.get("extended_hours") or {}
        return {
            "signal": signal, "used_weight": used_weight,
            "mu_return": mu_ret, "mu_relative_strength_vs_sox": mu_relative_strength_vs_sox,
            "mu_relative_strength_vs_qqq": mu_relative_strength_vs_qqq,
            "mu_data_status": _mu_data_status(mu),
            "sox_return": sox_ret, "nvda_return": nvda_ret, "amd_return": amd_ret,
            "avgo_return": avgo_ret, "qqq_return": qqq_ret,
            "mu_extended_hours": mu_ext,
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
        flow_signal = math.tanh(sum(flow_values) / len(flow_values) / 1_500_000.0) if flow_values else None

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
            "is_proxy": bool(investor.get("is_proxy", False)),
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
            "vwap": round(vwap, 1) if vwap else None,
            "vwap_position_pct": round(vwap_position_pct, 3) if vwap_position_pct is not None else None,
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
    # Stage 4.5: 회복(recovery) 점수 — "위험 지속" 관성 편향을 줄이기 위한 반등 신호.
    # 삼성전자/한미반도체/체결강도는 이 파이프라인에 수집되지 않아 unavailable로 둔다.
    # ------------------------------------------------------------------
    def _stage_recovery(self, market_data: dict, tech_indicators: dict, sector_stage: dict) -> dict:
        components: dict = {}

        components["hynix_vwap_reclaim_score"] = _norm01(sector_stage.get("vwap_position_pct"), 1.0)

        vol_change = tech_indicators.get("volume_change_pct")
        components["volume_confirmation_score"] = _norm01(vol_change, 30.0)

        rsi = tech_indicators.get("rsi_14")
        return_3d = tech_indicators.get("return_3d_pct")
        if rsi is not None and return_3d is not None:
            if rsi < 45.0 and return_3d > 0:
                components["rsi_rebound_score"] = 70.0
            elif rsi < 30.0:
                components["rsi_rebound_score"] = 55.0
            else:
                components["rsi_rebound_score"] = 50.0
        else:
            components["rsi_rebound_score"] = None

        domestic_index = market_data.get("domestic_index", {}) or {}
        kospi200_ret = domestic_index.get("kospi200_return")
        components["futures_rebound_score"] = _norm01(kospi200_ret, 1.0)

        investor = market_data.get("investor_flow", {}) or {}
        foreign_net = investor.get("foreign_net_buy")
        components["foreign_flow_score"] = _norm01(foreign_net, 500_000.0)

        # 이 파이프라인에는 삼성전자/한미반도체 개별 수집이 없어 항상 unavailable.
        components["samsung_confirmation_score"] = None
        components["hanmi_confirmation_score"] = None

        weights = {
            "hynix_vwap_reclaim_score": 0.30, "volume_confirmation_score": 0.15,
            "rsi_rebound_score": 0.20, "futures_rebound_score": 0.20, "foreign_flow_score": 0.15,
        }
        weighted, total_w = 0.0, 0.0
        for k, w in weights.items():
            v = components.get(k)
            if v is None:
                continue
            weighted += v * w
            total_w += w
        score = round(weighted / total_w, 2) if total_w > 0 else 50.0
        return {
            "recovery_score": score, "components": components,
            "unavailable": [k for k, v in components.items() if v is None],
        }

    # ------------------------------------------------------------------
    # 내일 시가 전용 산식 (spec: us_ai*0.30 + mu_rel*0.20 + close_location*0.15
    #                       + foreign_flow*0.15 + fx*0.10 + tomorrow_market*0.10)
    # ------------------------------------------------------------------
    def _compute_tomorrow_open_signal(
        self, stage_us: dict, stage_flow: dict, stage_sector: dict,
        tomorrow_market_prediction_factor: Optional[float], now_hm: Optional[str] = None,
    ) -> tuple:
        """
        내일 시가 산식. MU 장외(프리마켓/애프터마켓) score를 별도 항목으로 분리해
        25~35% 비중을 명시 배정한다(기존에는 us_ai_factor에 뭉쳐 있어 장외 흐름의
        영향력이 희석됐음). 08:30~09:00 KST(개장 직전)에는 이 비중을 추가로
        격상해 MU 장외 score를 사실상 핵심값으로 사용한다.

        Returns
        -------
        (composite_signal, weight_info: dict) — weight_info는 로깅/UI 표시용.
        """
        us_ai_factor = stage_us.get("signal") or 0.0
        mu_rel = stage_us.get("mu_relative_strength_vs_sox")
        mu_relative_strength_factor = max(-1.0, min(1.0, mu_rel / 3.0)) if mu_rel is not None else 0.0
        vwap_pos = stage_sector.get("vwap_position_pct")
        today_close_location_factor = max(-1.0, min(1.0, (vwap_pos or 0.0) / 1.0))
        foreign_net = stage_flow.get("foreign_net_buy")
        foreign_flow_factor = math.tanh(foreign_net / 1_500_000.0) if foreign_net is not None else 0.0
        usdkrw_change = stage_flow.get("usdkrw_change")
        fx_factor = max(-1.0, min(1.0, -usdkrw_change / 1.0)) if usdkrw_change is not None else 0.0
        tmp_factor = tomorrow_market_prediction_factor if tomorrow_market_prediction_factor is not None else 0.0

        mu_ext = stage_us.get("mu_extended_hours") or {}
        mu_ext_score = mu_ext.get("mu_extended_hours_score")
        mu_extended_hours_factor = max(-1.0, min(1.0, (mu_ext_score - 50.0) / 50.0)) if mu_ext_score is not None else None

        now_hm = now_hm or datetime.now().strftime("%H:%M")
        is_preopen_window = MU_EXTENDED_HOURS_PREOPEN_WINDOW[0] <= now_hm < MU_EXTENDED_HOURS_PREOPEN_WINDOW[1]
        mu_weight = MU_EXTENDED_HOURS_PREOPEN_WEIGHT if is_preopen_window else MU_EXTENDED_HOURS_TOMORROW_WEIGHT

        if mu_extended_hours_factor is None:
            # MU 장외 데이터가 전혀 없으면 그 비중을 us_ai_factor로 재배분한다(임의로 0 처리하지 않음).
            weights = {"us_ai": 0.30 + mu_weight, "mu_rel": 0.20, "close_loc": 0.15,
                       "foreign_flow": 0.10, "fx": 0.10, "tmp": 0.05}
            composite = (
                us_ai_factor * weights["us_ai"] + mu_relative_strength_factor * weights["mu_rel"]
                + today_close_location_factor * weights["close_loc"] + foreign_flow_factor * weights["foreign_flow"]
                + fx_factor * weights["fx"] + tmp_factor * weights["tmp"]
            )
            weight_info = {**weights, "mu_extended_hours": 0.0, "mu_extended_hours_unavailable": True, "preopen_window": is_preopen_window}
            return composite, weight_info

        remaining = 1.0 - mu_weight
        # 남은 비중을 기존 배분 비율(us_ai:mu_rel:close_loc:foreign:fx:tmp = 20:10:15:10:10:5=70)로 재분배.
        base_total = 0.20 + 0.10 + 0.15 + 0.10 + 0.10 + 0.05
        scale = remaining / base_total
        weights = {
            "us_ai": 0.20 * scale, "mu_rel": 0.10 * scale, "close_loc": 0.15 * scale,
            "foreign_flow": 0.10 * scale, "fx": 0.10 * scale, "tmp": 0.05 * scale,
        }
        composite = (
            mu_extended_hours_factor * mu_weight + us_ai_factor * weights["us_ai"]
            + mu_relative_strength_factor * weights["mu_rel"] + today_close_location_factor * weights["close_loc"]
            + foreign_flow_factor * weights["foreign_flow"] + fx_factor * weights["fx"] + tmp_factor * weights["tmp"]
        )
        weight_info = {**weights, "mu_extended_hours": mu_weight, "mu_extended_hours_unavailable": False, "preopen_window": is_preopen_window}
        return composite, weight_info

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
        now_hm: Optional[str] = None,
        market_collapse_score: Optional[float] = None,
        semiconductor_collapse_score: Optional[float] = None,
        tomorrow_market_prediction_factor: Optional[float] = None,
    ) -> dict:
        """
        Parameters
        ----------
        market_collapse_score, semiconductor_collapse_score : Market Regime
            Router에서 계산된 값(선택). 넘기면 collapse_penalty에 반영된다 —
            이 파이프라인은 독립적으로 실행 가능해야 하므로 넘기지 않아도
            정상 동작하며, 이 경우 collapse_penalty는 0으로 처리된다.
        tomorrow_market_prediction_factor : market_prediction.predict_tomorrow_market()의
            방향 신호(-1..1로 환산, 선택) — 내일 시가 산식의 10% 비중 항목.
        """
        tech_indicators = tech_indicators or {}
        micron_features = micron_features or {}
        market_data = market_data or {}

        stage_us = self._stage_us_ai_semi(market_data, micron_features)
        stage_flow = self._stage_domestic_flow(market_data)
        stage_sector = self._stage_domestic_sector(market_data, hynix_current_price, hynix_prev_close, tech_indicators)
        stage_self = self._stage_hynix_self(tech_indicators)
        stage_recovery = self._stage_recovery(market_data, tech_indicators, stage_sector)
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
        recovery_score = stage_recovery.get("recovery_score")

        mu_ext = market_data.get("mu", {}).get("extended_hours") or {}
        mu_ext_score = mu_ext.get("mu_extended_hours_score")
        mu_ext_source = mu_ext.get("data_source")
        mu_ext_freshness = mu_ext.get("freshness_seconds")
        mu_ext_is_delayed = bool(mu_ext.get("is_delayed"))
        mu_ext_available = mu_ext_score is not None
        # 자동매매 참고 가능 여부 — Yahoo/Naver(최후 보조)만으로는 내일시가/오전
        # 판단에 ML/자동매수 참고를 허용하지 않는다(명세 7절).
        mu_extended_hours_auto_trade_usable = mu_ext_available and not mu_ext_is_delayed and (
            mu_ext_freshness is None or mu_ext_freshness <= 300
        )

        data_quality_score, missing_warnings = _compute_data_quality(
            stage_results, holiday_mode, mu_is_stale, hynix_source, investor_source,
        )

        base_price, base_source = _resolve_price_anchor(hynix_current_price, hynix_prev_close)
        extreme_event = _is_extreme_event(stage_us)
        tomorrow_state = _tomorrow_state(now_hm)

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
            "recovery_score": recovery_score,
            "recovery_score_components": stage_recovery.get("components"),
            "recovery_score_unavailable": stage_recovery.get("unavailable"),
            "mu_data_status": stage_us.get("mu_data_status"),
            "mu_relative_strength_vs_sox": stage_us.get("mu_relative_strength_vs_sox"),
            "mu_relative_strength_vs_qqq": stage_us.get("mu_relative_strength_vs_qqq"),
            "mu_extended_hours_score": mu_ext_score,
            "mu_extended_hours_session_type": mu_ext.get("session_type"),
            "mu_extended_hours_data_source": mu_ext_source,
            "mu_extended_hours_is_realtime": mu_ext.get("is_realtime"),
            "mu_extended_hours_is_delayed": mu_ext_is_delayed,
            "mu_extended_hours_freshness_seconds": mu_ext_freshness,
            "mu_extended_hours_auto_trade_usable": mu_extended_hours_auto_trade_usable,
            "mu_extended_hours_confidence_penalty_reason": mu_ext.get("confidence_penalty_reason", []),
            "tomorrow_open_state": tomorrow_state,
            "key_reasons": _build_key_reasons(stage_results, stage_recovery),
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
                price_key = "predicted_close_today" if horizon == "close" else (
                    "predicted_open_tomorrow" if horizon == "tomorrow_open" else f"predicted_price_{horizon}"
                )
                result[price_key] = None
            _log_price_prediction(result)
            return result

        for horizon in HORIZONS:
            mu_ext_weight_info = None
            if horizon == "tomorrow_open":
                composite_signal, mu_ext_weight_info = self._compute_tomorrow_open_signal(
                    stage_us, stage_flow, stage_sector, tomorrow_market_prediction_factor, now_hm,
                )
            else:
                weights = HORIZON_WEIGHTS[horizon]
                composite, weight_sum = 0.0, 0.0
                for stage_name, w in weights.items():
                    sig = stage_results[stage_name].get("signal")
                    if sig is None:
                        continue
                    # MU 장외가 stale/delayed이면 us_ai_semi 신호의 기여도를 줄인다
                    # ("장중 하이닉스 예측에서는 MU 장외가 stale이면 가중치 축소").
                    if stage_name == "us_ai_semi" and mu_ext_available and (mu_ext_is_delayed or (mu_ext_freshness or 0) > 300):
                        w = w * 0.6
                    composite += sig * w
                    weight_sum += w
                composite_signal = composite / weight_sum if weight_sum > 1e-9 else 0.0

            # ── MU 장외 score를 관성 편향 완화와 동일한 방식으로 반영한다.
            # 강하면(>=65) 하락 편향을 보정(상향), 약하면(<=35) domestic_sector류 신호를 감점.
            mu_ext_adjustment = 0.0
            if mu_ext_score is not None and horizon != "tomorrow_open":
                influence = MU_EXTENDED_HOURS_INFLUENCE[horizon]
                if mu_ext_score >= 65.0:
                    mu_ext_adjustment = (mu_ext_score - 50.0) / 50.0 * influence
                elif mu_ext_score <= 35.0:
                    mu_ext_adjustment = (mu_ext_score - 50.0) / 50.0 * influence  # 음수 -> 감점 방향
                composite_signal += mu_ext_adjustment

            # ── 관성 편향 완화: recovery_score가 높을수록(특히 장기 horizon일수록)
            # 하락 방향으로 쏠린 composite_signal을 완화 방향으로 당긴다.
            recovery_adjustment = 0.0
            if recovery_score is not None and recovery_score > 50.0:
                recovery_adjustment = (recovery_score - 50.0) / 50.0 * HORIZON_RECOVERY_INFLUENCE[horizon]
                composite_signal += recovery_adjustment

            raw_return = composite_signal * HORIZON_RETURN_SCALE[horizon]

            # ── collapse_penalty: 시장 전체/반도체 섹터가 극단적 급락(>=70)이면
            # 추가 하락 페널티(market_collapse_score/semiconductor_collapse_score를
            # 넘겨받았을 때만 적용 — 넘기지 않으면 0).
            collapse_penalty = _compute_collapse_penalty(
                market_collapse_score, semiconductor_collapse_score, horizon,
            )
            raw_return -= collapse_penalty

            # ── rolling bias 보정: 최근 백테스트에서 확인된 이 horizon의 평균 오차를
            # 표본 수에 비례해(20건 미만 30%, 20~49건 70%, 50건 이상 100%) ±0.8% 이내로 반영.
            bias_correction = model_calibration.get_hynix_bias_correction(horizon, market_collapse_score)
            raw_return += bias_correction

            clip = HORIZON_CLIP_PCT[horizon] * (1.4 if extreme_event and horizon in ("close", "tomorrow_open") else 1.0)
            clipped_return = max(-clip, min(clip, raw_return))
            clip_applied = abs(raw_return - clipped_return) > 1e-9

            price = _round_krx(base_price * (1 + clipped_return / 100))
            p_up, p_side, p_down = _direction_probabilities(clipped_return, HORIZON_SIDEWAYS_PCT[horizon])
            direction = "UP" if clipped_return > 0 else ("DOWN" if clipped_return < 0 else "SIDEWAYS")
            # 가격 방향과 확률 방향은 동일한 clipped_return에서 파생되므로 항상 일치한다
            # (설계상 보장) — 명세의 "방향확률과 단일가격 방향 일관성" 요구를 만족한다.
            direction_price_consistent = not ((direction == "UP" and p_down > p_up) or (direction == "DOWN" and p_up > p_down))

            horizon_confidence = _compute_confidence(
                horizon=horizon, data_quality_score=data_quality_score, stage_results=stage_results,
                holiday_mode=holiday_mode, mu_is_stale=mu_is_stale, hynix_source=hynix_source,
                investor_is_proxy=stage_flow.get("is_proxy", False), recovery_score=recovery_score,
                predicted_direction=direction, tomorrow_state=tomorrow_state if horizon == "tomorrow_open" else None,
            )

            # ── MU 장외 데이터 상태에 따른 confidence 상한(명세 7절) — 내일시가/장
            # 개장전 판단일수록 강하게 적용하되, 장중 horizon에도 동일 원칙을 적용한다.
            if not mu_ext_available:
                horizon_confidence = min(horizon_confidence, 55.0)
            elif mu_ext_freshness is not None and mu_ext_freshness > 300:
                horizon_confidence = min(horizon_confidence, 60.0)
            elif mu_ext_is_delayed and horizon in ("tomorrow_open",):
                horizon_confidence = min(horizon_confidence, 60.0)

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
            result[f"direction_{suffix}"] = direction
            result[f"direction_price_consistent_{suffix}"] = direction_price_consistent
            result[f"recovery_adjustment_pct_{suffix}"] = round(recovery_adjustment * HORIZON_RETURN_SCALE[horizon], 3)
            result[f"collapse_penalty_pct_{suffix}"] = round(collapse_penalty, 3)
            result[f"rolling_bias_correction_pct_{suffix}"] = round(bias_correction, 3)
            result[f"mu_extended_hours_adjustment_pct_{suffix}"] = round(mu_ext_adjustment * HORIZON_RETURN_SCALE[horizon], 3)
            if mu_ext_weight_info is not None:
                result[f"mu_extended_hours_weight_{suffix}"] = mu_ext_weight_info.get("mu_extended_hours")
                _log_mu_reflected_weight(mu_ext, mu_ext_weight_info.get("mu_extended_hours"))

        # 사용자 요구 명세의 별칭 필드(내일 확률은 "_tomorrow"로도 노출)
        result["probability_up_tomorrow"] = result["probability_up_tomorrow_open"]
        result["probability_down_tomorrow"] = result["probability_down_tomorrow_open"]
        result["probability_sideways_tomorrow"] = result["probability_sideways_tomorrow_open"]
        result["confidence_tomorrow_open_alias"] = result["confidence_tomorrow_open"]

        result["message"] = f"데이터 품질 {data_quality_score:.0f}/100 — 다중 horizon 예측 완료"
        _log_price_prediction(result)
        return result


def _compute_collapse_penalty(market_collapse_score, semiconductor_collapse_score, horizon: str) -> float:
    scores = [s for s in (market_collapse_score, semiconductor_collapse_score) if s is not None]
    if not scores:
        return 0.0
    worst = max(scores)
    if worst < 70.0:
        return 0.0
    intensity = min(1.0, (worst - 70.0) / 30.0)
    return round(intensity * HORIZON_COLLAPSE_PENALTY_SCALE.get(horizon, 0.0), 4)


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


def _compute_signal_consistency(stage_results: dict) -> float:
    """4단계 신호의 방향(부호)이 서로 얼마나 일치하는지(0~100)."""
    signals = [sr.get("signal") for sr in stage_results.values() if sr.get("signal") is not None]
    if len(signals) < 2:
        return 50.0
    positive = sum(1 for s in signals if s > 0.05)
    negative = sum(1 for s in signals if s < -0.05)
    agreement = max(positive, negative) / len(signals)
    return round(agreement * 100.0, 1)


def _compute_recent_backtest_accuracy_score(horizon: str) -> float:
    """model_calibration에 쌓인 표본 수를 신뢰도 점수로 변환(표본 없으면 중립 50)."""
    info = model_calibration.get_hynix_bias_info(horizon)
    n = info.get("sample_count", 0)
    if n <= 0:
        return 50.0
    return round(min(90.0, 50.0 + n * 0.8), 1)


def _compute_freshness_score(holiday_mode: bool, mu_is_stale: bool, hynix_source) -> float:
    score = 95.0
    if mu_is_stale:
        score -= 25.0
    if hynix_source not in ("KIS", "kis"):
        score -= 10.0
    if holiday_mode:
        score -= 10.0
    return round(max(0.0, min(100.0, score)), 1)


def _compute_confidence(
    horizon: str, data_quality_score: float, stage_results: dict, holiday_mode: bool,
    mu_is_stale: bool, hynix_source, investor_is_proxy: bool,
    recovery_score: Optional[float], predicted_direction: str, tomorrow_state: Optional[str],
) -> float:
    """
    confidence = data_quality*0.30 + signal_consistency*0.25 + horizon_reliability*0.15
                 + recent_backtest_accuracy*0.15 + freshness*0.15, 이후 상한 캡 적용.
    """
    signal_consistency = _compute_signal_consistency(stage_results)
    horizon_reliability = HORIZON_RELIABILITY_CAP[horizon]
    backtest_accuracy = _compute_recent_backtest_accuracy_score(horizon)
    freshness = _compute_freshness_score(holiday_mode, mu_is_stale, hynix_source)

    confidence = (
        data_quality_score * 0.30 + signal_consistency * 0.25 + horizon_reliability * 0.15
        + backtest_accuracy * 0.15 + freshness * 0.15
    )

    core_missing = sum(1 for sr in stage_results.values() if (sr.get("used_weight") or 0.0) < 0.3)
    if core_missing >= 3:
        confidence = min(confidence, 60.0)
    if investor_is_proxy:
        confidence = min(confidence, 75.0)
    mu_source = stage_results["us_ai_semi"]["sources"].get("mu")
    if horizon == "tomorrow_open" and mu_source == "yahoo":
        confidence = min(confidence, 60.0)
    if holiday_mode:
        confidence = min(confidence, 85.0)
    if tomorrow_state == "INTRADAY_PRELIMINARY":
        confidence = min(confidence, 65.0)
    if recovery_score is not None:
        if (predicted_direction == "DOWN" and recovery_score >= 70.0) or (predicted_direction == "UP" and recovery_score <= 30.0):
            confidence -= 10.0

    return round(max(0.0, min(100.0, confidence)), 1)


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


def _build_key_reasons(stage_results: dict, stage_recovery: Optional[dict] = None) -> list:
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
    if stage_recovery and stage_recovery.get("recovery_score") is not None:
        rs = stage_recovery["recovery_score"]
        if rs >= 60.0 or rs <= 40.0:
            candidates.append((abs(rs - 50) * 0.6, f"회복(recovery) 점수 {rs:.0f}/100"))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in candidates[:5]]


def _log_mu_reflected_weight(mu_ext: dict, reflected_weight: Optional[float]) -> None:
    """MU 장외 데이터가 실제로 하이닉스 내일시가 예측에 반영된 비중을
    logs/mu_extended_hours/YYYYMMDD.jsonl에 추가 기록한다(수집시점 로그와
    별개 — collect_mu_extended_hours()가 남기는 항목은 reflected_weight=None).
    """
    if not mu_ext:
        return
    try:
        MU_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = MU_LOG_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"), "session_type": mu_ext.get("session_type"),
            "price": mu_ext.get("current_price"), "bar_1m": mu_ext.get("bar_1m"), "bar_3m": mu_ext.get("bar_3m"),
            "slope_3m": mu_ext.get("slope_3m"), "slope_15m": mu_ext.get("slope_15m"),
            "score": mu_ext.get("mu_extended_hours_score"), "source": mu_ext.get("data_source"),
            "freshness": mu_ext.get("freshness_seconds"), "reflected_hynix_prediction_weight": reflected_weight,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("[HynixPricePredictor] MU 반영비중 로그 실패(무해): %s", exc)


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
            "recovery_score": result.get("recovery_score"),
            "tomorrow_open_state": result.get("tomorrow_open_state"),
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
    now_hm: Optional[str] = None,
    market_collapse_score: Optional[float] = None,
    semiconductor_collapse_score: Optional[float] = None,
    tomorrow_market_prediction_factor: Optional[float] = None,
) -> dict:
    """모듈 수준 진입점 — HynixPricePredictor().predict()의 얇은 래퍼."""
    return HynixPricePredictor().predict(
        market_data=market_data,
        hynix_current_price=hynix_current_price,
        hynix_prev_close=hynix_prev_close,
        tech_indicators=tech_indicators,
        micron_features=micron_features,
        now_hm=now_hm,
        market_collapse_score=market_collapse_score,
        semiconductor_collapse_score=semiconductor_collapse_score,
        tomorrow_market_prediction_factor=tomorrow_market_prediction_factor,
    )
