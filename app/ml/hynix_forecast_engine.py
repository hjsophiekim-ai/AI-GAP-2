"""Forecast pipeline and data-quality gate for the SK Hynix tab."""

from __future__ import annotations

import logging
import json
from datetime import datetime
from pathlib import Path

BLOCK_THRESHOLD = 0.40
LOW_CONF_THRESHOLD = 0.70
CONFIDENCE_GATE = 40.0

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs"


def _get_debug_logger() -> logging.Logger:
    logger = logging.getLogger("hynix_prediction_debug")
    if not logger.handlers:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOG_DIR / "hynix_prediction_debug.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


def _check_minimum_conditions(auto_feat: dict) -> tuple[bool, str]:
    """Validate required inputs before any forecast is created."""
    predictor_kwargs = auto_feat.get("predictor_kwargs", {})
    micron_features = auto_feat.get("micron_features", {})

    current_price = predictor_kwargs.get("hynix_current_price")
    prev_close = predictor_kwargs.get("hynix_prev_close")
    daily_count = auto_feat.get("hynix_daily_count")

    if "hynix_current_price" in predictor_kwargs and current_price is None:
        return False, "SK Hynix current price missing; prediction blocked"
    if prev_close is None:
        return False, "SK하이닉스 전일 종가 missing; prediction blocked"
    if daily_count is not None and int(daily_count) < 20:
        return False, f"SK Hynix daily candles={daily_count}; at least 20 required"

    has_mu = micron_features.get("micron_session_strength_score") is not None
    has_kospilab = predictor_kwargs.get("kospilab_expected_return_pct") is not None
    if not has_mu and not has_kospilab:
        return False, "MU and 코스피랩 inputs are both missing; prediction blocked"

    external_values = [
        predictor_kwargs.get("sox_return_pct"),
        predictor_kwargs.get("nvda_return_pct"),
        predictor_kwargs.get("qqq_return_pct"),
        predictor_kwargs.get("usd_krw_change_pct"),
    ]
    external_count = sum(1 for value in external_values if value is not None)
    if external_count < 2:
        return False, f"Only {external_count} 외부 지표 collected; at least 2 required"

    return True, "ok"


def run_forecast(market_data: dict) -> dict:
    """Build features, apply gates, and run price/swing forecasts."""
    log = _get_debug_logger()
    result = {
        "status": "blocked",
        "data_quality": 0.0,
        "confidence_blocked": False,
        "message": "data collection pending",
        "auto_features": None,
        "prediction": None,
        "swing": None,
        "explanation": None,
        "price_prediction": None,
        "ml_prediction": None,
        "ensemble_prediction": None,
        "errors": list(market_data.get("errors", [])),
        "diagnostics": _build_diagnostics(market_data),
    }

    gate_ok, gate_msg = _check_market_data_gate(market_data)
    if not gate_ok:
        result["message"] = gate_msg
        result["errors"].append(gate_msg)
        _log_used_inputs(market_data, result)
        return result

    try:
        from app.features.hynix_auto_features import build_auto_features

        auto_feat = build_auto_features(market_data)
        result["auto_features"] = auto_feat
        data_quality = float(auto_feat.get("data_quality", 0.0))
        result["data_quality"] = data_quality
    except Exception as exc:
        result["message"] = f"Feature build failed: {exc}"
        result["errors"].append(str(exc))
        log.exception("feature build failed")
        _log_used_inputs(market_data, result)
        return result

    min_ok, min_msg = _check_minimum_conditions(result["auto_features"])
    if not min_ok:
        result["message"] = min_msg
        log.warning("minimum conditions failed: %s", min_msg)
        result["errors"].append(min_msg)
        _log_used_inputs(market_data, result)
        return result

    if result["data_quality"] < BLOCK_THRESHOLD:
        result["message"] = (
            f"Data quality {result['data_quality'] * 100:.0f}% is below "
            f"{BLOCK_THRESHOLD * 100:.0f}%; prediction blocked"
        )
        result["errors"].append(result["message"])
        _log_used_inputs(market_data, result)
        return result

    try:
        from app.models.hynix_predictor import predict_hynix

        pk = result["auto_features"]["predictor_kwargs"]
        log.warning(
            "[PREDICT_BASE] base_price=%s current_price=%s prev_close=%s",
            pk.get("hynix_current_price"),
            pk.get("hynix_current_price"),
            pk.get("hynix_prev_close"),
        )
        prediction = predict_hynix(
            micron_features=result["auto_features"]["micron_features"],
            **result["auto_features"]["predictor_kwargs"],
        )
        result["prediction"] = prediction
        abnormal = _analyze_prediction_gap(prediction, pk.get("hynix_current_price"))
        if abnormal:
            result["diagnostics"]["prediction_gap_analysis"] = abnormal
            result["errors"].append(abnormal["message"])
            result["message"] = abnormal["message"]
            log.warning("prediction gap analysis: %s", abnormal)
            _log_used_inputs(market_data, result)
            return result
    except Exception as exc:
        result["message"] = f"Prediction failed: {exc}"
        result["errors"].append(str(exc))
        log.exception("prediction failed")
        _log_used_inputs(market_data, result)
        return result

    # 다중 horizon(30분/1시간/3시간/오늘종가/내일시가) 가격 예측 — 기존 오늘/내일/3일/2주
    # 예측(predict_hynix)과 독립적인 추가 결과이며, 실패해도 기존 파이프라인 상태에는
    # 영향을 주지 않는다(앱이 죽지 않도록 항상 try/except로 격리).
    try:
        from app.models.hynix_price_predictor import predict_hynix_multi_horizon

        result["price_prediction"] = predict_hynix_multi_horizon(
            market_data=market_data,
            hynix_current_price=pk.get("hynix_current_price"),
            hynix_prev_close=pk.get("hynix_prev_close"),
            tech_indicators=result["auto_features"].get("tech_indicators"),
            micron_features=result["auto_features"]["micron_features"],
        )
    except Exception as exc:
        result["price_prediction"] = None
        result["errors"].append(f"Multi-horizon price prediction failed: {exc}")
        log.exception("multi-horizon price prediction failed")

    # ML(1년치 학습) 예측 + 룰/ML 앙상블 — 완전히 추가적(additive)이며 실패해도
    # 기존 파이프라인(price_prediction 포함)에는 영향을 주지 않는다. 학습된
    # 모델이 아직 없으면(scripts/train_hynix_models.py 실행 전) ml_prediction은
    # available=False로 채워지고 ensemble은 Rule 100%로 자동 대체된다.
    result["ml_prediction"] = None
    result["ensemble_prediction"] = None
    if result.get("price_prediction") is not None:
        try:
            from app.ml.historical_data_loader import load_all_from_cache
            from app.ml.hynix_ml_predictor import predict_all_horizons_ml
            from app.ml.ensemble_predictor import build_ensemble_result
            from app.market import us_market_data as umd

            historical_cache = load_all_from_cache()
            ml_result = predict_all_horizons_ml(historical_cache)
            try:
                us_status = umd.get_us_market_status()
                holiday_mode = bool(us_status.get("is_us_holiday") or us_status.get("is_us_weekend"))
            except Exception:
                holiday_mode = False

            result["ml_prediction"] = ml_result
            result["ensemble_prediction"] = build_ensemble_result(
                result["price_prediction"], ml_result, holiday_mode=holiday_mode,
            )
        except Exception as exc:
            result["errors"].append(f"ML/ensemble prediction failed: {exc}")
            log.exception("ML/ensemble prediction failed")

    try:
        from app.models.hynix_swing_flag import evaluate_swing_flag

        swing = evaluate_swing_flag(
            micron_features=result["auto_features"]["micron_features"],
            prediction=result["prediction"],
            **result["auto_features"]["swing_kwargs"],
        )
        result["swing"] = swing
        contradiction = _find_signal_contradiction(swing)
        if contradiction:
            swing["action_text"] = None
            swing["buy_timing_text"] = None
            swing["sell_timing_text"] = None
            swing["signal_blocked"] = True
            swing["signal_block_reason"] = contradiction
            result["errors"].append(contradiction)

        from app.data.market_data_validator import validate_prediction_prices, validate_swing_result

        zone_ok, zone_msg = validate_swing_result(swing)
        if not zone_ok:
            result["errors"].append(f"Invalid price zones: {zone_msg}")
        current_price = result["auto_features"]["predictor_kwargs"].get("hynix_current_price")
        validate_prediction_prices({**prediction, **{
            "target_price": swing.get("target_price"),
            "stop_loss_price": swing.get("stop_loss_price"),
        }}, current_price)
    except Exception as exc:
        result["errors"].append(f"Swing flag failed: {exc}")
        result["message"] = f"Price validation failed: {exc}"
        log.exception("swing flag failed")
        _log_used_inputs(market_data, result)
        return result

    swing_confidence = (result["swing"] or {}).get("confidence_score", 0.0)
    if swing_confidence < CONFIDENCE_GATE:
        result["confidence_blocked"] = True

    try:
        from app.models.hynix_swing_explainer import generate_swing_explanation

        if result["swing"]:
            result["explanation"] = generate_swing_explanation(
                swing_result=result["swing"],
                micron_features=result["auto_features"]["micron_features"],
                tech_indicators=result["auto_features"].get("tech_indicators"),
                kospilab_return=result["auto_features"].get("kospilab_return"),
            )
    except Exception as exc:
        result["errors"].append(f"Explanation failed: {exc}")

    if result["data_quality"] < LOW_CONF_THRESHOLD:
        result["status"] = "low_confidence"
        result["message"] = f"Data quality {result['data_quality'] * 100:.0f}%; low-confidence forecast"
    else:
        result["status"] = "ok"
        result["message"] = f"Data quality {result['data_quality'] * 100:.0f}%; forecast complete"

    _log_used_inputs(market_data, result)
    return result


def _build_diagnostics(market_data: dict) -> dict:
    mu = market_data.get("mu", {})
    nvda = market_data.get("nvda", {})
    index = market_data.get("index", {})
    hynix = market_data.get("hynix", {})
    kospilab = market_data.get("kospilab", {})

    df_daily = hynix.get("df_daily")
    daily_count = 0 if df_daily is None else len(df_daily)
    source_detail = hynix.get("source_detail", {})
    index_sources = index.get("source_detail", {})

    return {
        "hynix_current": {
            "ok": hynix.get("current_price") is not None,
            "source": source_detail.get("current_price") or hynix.get("source"),
            "value": hynix.get("current_price"),
            "error": hynix.get("error"),
        },
        "hynix_daily": {
            "ok": daily_count >= 20 and hynix.get("prev_close") is not None,
            "source": source_detail.get("daily_ohlcv") or hynix.get("source"),
            "count": daily_count,
            "prev_close": hynix.get("prev_close"),
            "error": hynix.get("error"),
        },
        "hynix": {
            "ok": daily_count >= 20 and hynix.get("prev_close") is not None,
            "source": hynix.get("source"),
            "prev_close": hynix.get("prev_close"),
            "error": hynix.get("error"),
        },
        "mu": {"ok": mu.get("current_price") is not None, "source": mu.get("source"), "status": mu.get("current_price_status"), "error": mu.get("error")},
        "mu_1min": {"ok": mu.get("df_1min") is not None, "source": mu.get("source"), "status": mu.get("minute_1m_status"), "error": mu.get("minute_error")},
        "mu_3min": {"ok": mu.get("df_3min") is not None, "source": mu.get("source"), "status": mu.get("minute_3m_status"), "error": mu.get("minute_error")},
        "mu_daily": {"ok": mu.get("df_daily") is not None, "source": mu.get("source"), "status": mu.get("daily_status"), "error": mu.get("minute_error")},
        "nvda": {"ok": nvda.get("current_price") is not None, "source": nvda.get("source"), "error": nvda.get("error")},
        "sox": {"ok": index.get("sox_return") is not None, "source": index_sources.get("SOXX") or index.get("source"), "value": index.get("sox_return")},
        "qqq": {"ok": index.get("qqq_return") is not None, "source": index_sources.get("NASDAQ_FUTURES") or index_sources.get("QQQ") or index.get("source"), "value": index.get("qqq_return")},
        "usdkrw": {"ok": index.get("usdkrw_change") is not None, "source": index_sources.get("USDKRW") or index.get("source"), "value": index.get("usdkrw_change")},
        "kospilab": {
            "ok": kospilab.get("source_status") == "success" and kospilab.get("hynix_reference_return") is not None,
            "status": kospilab.get("source_status"),
            "error": kospilab.get("error_message"),
        },
    }


def _check_market_data_gate(market_data: dict) -> tuple[bool, str]:
    hynix = market_data.get("hynix", {})
    identity = hynix.get("stock_identity", {})
    if identity and not identity.get("ok"):
        return False, f"Stock identity validation failed: {identity.get('message')}"

    # collect_hynix_daily()는 API(KIS) 우선 -> 네이버 -> yfinance 순으로 개별
    # 소스 검증만 통과하면 성공을 반환한다(소스간 가격 불일치는 경고로만 남김).
    # 따라서 여기서는 교차검증 결과(price_validation.ok)가 아니라 실제 수집된
    # 데이터(current_price/일봉 개수)로만 게이트를 판단한다 — 그래야 정상적으로
    # 수집된 단일 소스 데이터가 교차검증 불일치 때문에 차단되지 않는다.
    if hynix.get("current_price") is None:
        price_validation = hynix.get("price_validation") or {}
        return False, f"SK Hynix current price collection failed: {price_validation.get('message', hynix.get('error'))}"
    daily_count = 0 if hynix.get("df_daily") is None else len(hynix.get("df_daily"))
    if daily_count < 20 or hynix.get("prev_close") is None:
        return False, f"SK Hynix daily candle collection failed: valid candles={daily_count} (need >= 20)"

    mu = market_data.get("mu", {})
    if mu.get("df_1min") is None:
        return False, "MU 1-minute candles missing; prediction unavailable"
    if mu.get("df_3min") is None:
        return False, "MU 3-minute candles missing; prediction unavailable"
    if mu.get("df_daily") is None:
        return False, "MU daily candles missing; prediction unavailable"

    index = market_data.get("index", {})
    missing = []
    if index.get("sox_return") is None:
        missing.append("SOX")
    if index.get("qqq_return") is None:
        missing.append("Nasdaq futures")
    if index.get("usdkrw_change") is None:
        missing.append("USD/KRW")
    if missing:
        return False, "Required overseas market data missing: " + ", ".join(missing)
    return True, "ok"


def _find_signal_contradiction(swing: dict) -> str | None:
    score = float(swing.get("swing_score") or 50.0)
    flag = str(swing.get("swing_flag") or "")
    bullish = {"STRONG_BUY", "BUY", "WAIT_BUY"}
    bearish = {"TAKE_PROFIT", "SELL", "STRONG_SELL"}
    if score >= 55 and flag in bearish:
        return f"Swing Score {score:.1f} is bullish but signal is {flag}; trade signal blocked"
    if score <= 45 and flag in bullish:
        return f"Swing Score {score:.1f} is bearish but signal is {flag}; trade signal blocked"
    return None


def _analyze_prediction_gap(prediction: dict, current_price: float | None) -> dict | None:
    if not current_price:
        return {"message": "Prediction gap analysis failed: current price missing", "cause": "missing_current_price"}
    expected = prediction.get("today_close_expected")
    if expected is None:
        return None
    gap_pct = (float(expected) / float(current_price) - 1.0) * 100
    if abs(gap_pct) <= 15.0:
        return None
    causes = []
    if prediction.get("base_price_source") != "current_price":
        causes.append("prediction was not anchored to current_price")
    if prediction.get("current_price") != current_price:
        causes.append("current_price mismatch between input and prediction")
    if not causes:
        causes.append("return model produced an abnormal move")
    return {
        "message": f"Prediction unavailable: expected price differs from current price by {gap_pct:.2f}%",
        "gap_pct": round(gap_pct, 4),
        "current_price": current_price,
        "expected_price": expected,
        "cause": "; ".join(causes),
    }


def _log_used_inputs(market_data: dict, result: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "logged_at": datetime.now().isoformat(),
            "status": result.get("status"),
            "message": result.get("message"),
            "market_data_collected_at": market_data.get("collected_at"),
            "diagnostics": result.get("diagnostics"),
            "auto_features": result.get("auto_features"),
            "prediction": result.get("prediction"),
            "swing": result.get("swing"),
        }
        with (LOG_DIR / "hynix_prediction_inputs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logging.getLogger("hynix_prediction_debug").exception("used input log failed")


def _filter_kwargs(kwargs: dict, blocked_keys: set[str]) -> dict:
    return {key: value for key, value in kwargs.items() if key not in blocked_keys}


def collection_rate_label(data_quality: float) -> tuple[str, str]:
    if data_quality >= LOW_CONF_THRESHOLD:
        return "정상", "#2ecc71"
    if data_quality >= BLOCK_THRESHOLD:
        return "낮은 신뢰도", "#e67e22"
    return "수집 부족", "#e74c3c"
