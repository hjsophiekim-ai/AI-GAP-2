"""backtest_hynix_ml.py — Rule/ML/Ensemble 예측을 과거 데이터로 검증한다.

사용법:
    python scripts/backtest_hynix_ml.py

출력:
    reports/hynix_ml_backtest_summary.md
    reports/hynix_ml_backtest_detail.csv

방법:
  각 horizon의 walk-forward 테스트 구간(hynix_ml_trainer와 동일하게 시간순
  마지막 20%)에서, 그 시점의 feature row로부터
    - ML 예측: 이미 학습된 모델(model_registry)의 예측
    - Rule 예측: feature row를 hynix_price_predictor가 쓰는 market_data/
      tech_indicators/micron_features 형태로 근사 재구성해 실행한 결과
      (분봉 기반 stage 일부는 과거 분봉 원본이 없어 근사/생략됨 — 한계로 명시)
    - Ensemble 예측: 위 둘을 ensemble_predictor로 합성
  를 모두 계산해 실제값(actual_return)과 비교한다.

주의: 이 스크립트는 하이닉스 예측 로그(logs/hynix_prediction/*.jsonl)를
과거 데이터로 수백 번 오염시키지 않도록, 백테스트 동안 룰 예측기의 로그
기록을 임시로 비활성화한다(스크립트 종료 시 자동 복원).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.ml import feature_builder as fb  # noqa: E402
from app.ml import model_registry as registry  # noqa: E402
from app.ml.ensemble_predictor import ensemble_horizon, load_ensemble_config  # noqa: E402
from app.ml.historical_data_loader import load_all_from_cache  # noqa: E402

REPORTS_DIR = ROOT / "reports"
DAILY_HORIZONS = ("close", "next_open")
INTRADAY_HORIZONS = ("30m", "1h", "3h")
ALL_HORIZONS = ("30m", "1h", "3h", "close", "next_open")

MAPE_TARGET_PCT = {"30m": 0.8, "1h": 1.2, "3h": 1.8, "close": 1.5, "next_open": 2.0}
DIRECTION_ACCURACY_TARGET = 0.60


def _reconstruct_rule_inputs(row: pd.Series) -> tuple:
    market_data = {
        "mu": {"source": "historical", "is_stale": False},
        "nvda": {"source": "historical", "regular_return": row.get("nvda_return")},
        "amd": {"source": "historical", "regular_return": row.get("amd_return")},
        "avgo": {"source": "historical", "regular_return": row.get("avgo_return")},
        "index": {"source": "historical", "sox_return": row.get("soxx_or_smh_return"),
                   "qqq_return": row.get("qqq_return"), "usdkrw_change": row.get("usdkrw_return")},
        "domestic_index": {"source": "historical", "kospi_return": row.get("kospi_return"),
                             "kospi200_return": row.get("kospi200_futures_return")},
        "investor_flow": {"source": "historical", "foreign_net_buy": None, "institution_net_buy": None},
        "hynix": {"source": "historical"},
        "hynix_minute": {"source": "historical", "df_1min": None},
    }
    tech_indicators = {
        "rsi_14": row.get("hynix_rsi") if "hynix_rsi" in row.index else row.get("rsi"),
        "macd_signal_cross": None, "ma5_position_pct": None, "ma20_position_pct": None,
        "from_20d_high_pct": None,
        "return_3d_pct": row.get("hynix_return_3d") if "hynix_return_3d" in row.index else None,
        "volume_change_pct": None, "bollinger_pct": None,
    }
    micron_features = {"micron_regular_return": row.get("mu_return")}
    return market_data, tech_indicators, micron_features


def _rule_predict_return(predictor, row: pd.Series, horizon: str) -> float | None:
    close_col = "hynix_close" if "hynix_close" in row.index else "close"
    current_price = row.get(close_col)
    if current_price is None or pd.isna(current_price):
        return None
    market_data, tech, mf = _reconstruct_rule_inputs(row)
    try:
        result = predictor.predict(
            market_data=market_data, hynix_current_price=float(current_price),
            hynix_prev_close=float(current_price), tech_indicators=tech, micron_features=mf,
        )
    except Exception:
        return None
    key = "expected_return_pct_tomorrow_open" if horizon == "next_open" else f"expected_return_pct_{horizon}"
    return result.get(key)


def _ml_predict_row(horizon: str, row: pd.Series) -> dict:
    reg_model, reg_meta = registry.load_model(horizon, "regressor")
    if reg_model is None or reg_meta is None:
        return {"available": False}
    feature_columns = reg_meta.get("feature_columns", [])
    medians = reg_meta.get("train_medians", {})
    values = []
    for c in feature_columns:
        v = row.get(c)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            v = medians.get(c, 0.0)
        values.append(float(v))
    X = pd.DataFrame([values], columns=feature_columns)
    try:
        pred = float(reg_model.predict(X)[0])
    except Exception:
        return {"available": False}
    return {"available": True, "predicted_return_pct": round(pred, 4), "model_confidence": 65.0,
            "below_min_samples": reg_meta.get("below_min_samples", True),
            "backtest_metrics": {"direction": (reg_meta.get("metrics") or {})}}


def _price_mape(actual_returns: np.ndarray, predicted_returns: np.ndarray, base_prices: np.ndarray) -> float:
    """가격 기준 MAPE. 수익률(%) 기준으로 계산하면 실제값이 0%에 가까울 때
    분모가 0에 가까워져 값이 폭발한다(전형적인 MAPE 병리) — 항상 base_price와
    함께 실제 가격/예측 가격으로 환산한 뒤 계산해야 한다."""
    actual_price = base_prices * (1 + actual_returns / 100)
    predicted_price = base_prices * (1 + predicted_returns / 100)
    denom = np.where(np.abs(actual_price) < 1e-6, 1e-6, np.abs(actual_price))
    return float(np.mean(np.abs((actual_price - predicted_price) / denom)) * 100)


def _direction_of(v: float, band: float) -> str:
    if v > band:
        return "UP"
    if v < -band:
        return "DOWN"
    return "SIDEWAYS"


def _evaluate_method(actual_returns: list, predicted_returns: list, base_prices: list, band: float) -> dict:
    a = np.array(actual_returns, dtype=float)
    p = np.array(predicted_returns, dtype=float)
    b = np.array(base_prices, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(p) | np.isnan(b) | (b <= 0))
    a, p, b = a[mask], p[mask], b[mask]
    if len(a) == 0:
        return {"n": 0}
    mae = float(np.mean(np.abs(a - p)))
    rmse = float(np.sqrt(np.mean((a - p) ** 2)))
    mape = _price_mape(a, p, b)
    actual_dir = [_direction_of(v, band) for v in a]
    pred_dir = [_direction_of(v, band) for v in p]
    direction_acc = float(np.mean([ad == pd_ for ad, pd_ in zip(actual_dir, pred_dir)]))

    up_tp = sum(1 for ad, pdv in zip(actual_dir, pred_dir) if ad == "UP" and pdv == "UP")
    up_pred = sum(1 for pdv in pred_dir if pdv == "UP")
    up_actual = sum(1 for ad in actual_dir if ad == "UP")
    down_tp = sum(1 for ad, pdv in zip(actual_dir, pred_dir) if ad == "DOWN" and pdv == "DOWN")
    down_pred = sum(1 for pdv in pred_dir if pdv == "DOWN")
    down_actual = sum(1 for ad in actual_dir if ad == "DOWN")

    return {
        "n": len(a), "mae": round(mae, 4), "rmse": round(rmse, 4), "mape_pct": round(mape, 4),
        "direction_accuracy": round(direction_acc, 4),
        "up_precision": round(up_tp / up_pred, 4) if up_pred else None,
        "up_recall": round(up_tp / up_actual, 4) if up_actual else None,
        "down_precision": round(down_tp / down_pred, 4) if down_pred else None,
        "down_recall": round(down_tp / down_actual, 4) if down_actual else None,
    }


def backtest_horizon(horizon: str, table: pd.DataFrame) -> dict:
    from app.models.hynix_price_predictor import HynixPricePredictor
    import app.models.hynix_price_predictor as hpp_module

    reg_col, dir_col = f"target_return_{horizon}", f"target_direction_{horizon}"
    band = fb.SIDEWAYS_BAND_PCT[horizon]
    valid = table.dropna(subset=[reg_col]).sort_values("datetime").reset_index(drop=True)
    n = len(valid)
    if n < 10:
        return {"horizon": horizon, "error": f"표본 부족({n})", "rows": []}

    split_idx = int(n * 0.8)
    test_df = valid.iloc[split_idx:].copy()
    if test_df.empty:
        test_df = valid.copy()

    predictor = HynixPricePredictor()
    original_log_fn = hpp_module._log_price_prediction
    hpp_module._log_price_prediction = lambda result: None  # 백테스트 중 실로그 오염 방지

    rows = []
    try:
        close_col = "hynix_close" if "hynix_close" in test_df.columns else "close"
        for _, row in test_df.iterrows():
            actual = row[reg_col]
            base_price = float(row.get(close_col, 0) or 0) or None
            rule_ret = _rule_predict_return(predictor, row, horizon)
            ml_info = _ml_predict_row(horizon, row)
            ml_ret = ml_info.get("predicted_return_pct") if ml_info.get("available") else None

            ens = ensemble_horizon(
                horizon if horizon != "next_open" else "next_open",
                rule_result=_rule_result_stub(rule_ret, horizon),
                ml_result={"horizons": {horizon: ml_info}},
                base_price=base_price,
                holiday_mode=False,
            )
            rows.append({
                "datetime": row["datetime"], "actual_return_pct": actual, "base_price": base_price,
                "rule_return_pct": rule_ret, "ml_return_pct": ml_ret,
                "ensemble_return_pct": ens.get("ensemble_return_pct"),
            })
    finally:
        hpp_module._log_price_prediction = original_log_fn

    return {"horizon": horizon, "rows": rows, "band": band}


def _rule_result_stub(rule_return: float | None, horizon: str) -> dict:
    key = "expected_return_pct_tomorrow_open" if horizon == "next_open" else f"expected_return_pct_{horizon}"
    price_key = "predicted_open_tomorrow" if horizon == "next_open" else (
        "predicted_close_today" if horizon == "close" else f"predicted_price_{horizon}"
    )
    return {"base_price": None, key: rule_return, price_key: None}


def _window_mask(rows: list, days: int) -> list:
    if not rows:
        return []
    max_dt = max(r["datetime"] for r in rows)
    cutoff = max_dt - pd.Timedelta(days=days)
    return [r for r in rows if r["datetime"] > cutoff]


def summarize_backtest(horizon_results: dict) -> dict:
    summary: dict = {"horizons": {}}
    for horizon, data in horizon_results.items():
        rows = data.get("rows", [])
        band = data.get("band", fb.SIDEWAYS_BAND_PCT.get(horizon, 1.0))
        if not rows:
            summary["horizons"][horizon] = {"error": data.get("error", "표본 없음")}
            continue

        windows = {"full": rows, "recent_3m": _window_mask(rows, 90), "recent_1m": _window_mask(rows, 30)}
        window_results = {}
        for win_name, win_rows in windows.items():
            methods = {}
            for method in ("rule", "ml", "ensemble"):
                actual = [r["actual_return_pct"] for r in win_rows]
                predicted = [r.get(f"{method}_return_pct") for r in win_rows]
                base_prices = [r.get("base_price") for r in win_rows]
                methods[method] = _evaluate_method(actual, predicted, base_prices, band)
            window_results[win_name] = methods
        summary["horizons"][horizon] = {"windows": window_results, "n_total": len(rows)}
    return summary


def _meets_bar(horizon: str, ensemble_metrics: dict) -> bool:
    mape = ensemble_metrics.get("mape_pct")
    acc = ensemble_metrics.get("direction_accuracy")
    if mape is None or acc is None:
        return False
    return mape <= MAPE_TARGET_PCT[horizon] and acc >= DIRECTION_ACCURACY_TARGET


def write_reports(summary: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    detail_rows = []
    for horizon, info in summary["horizons"].items():
        if "error" in info or "windows" not in info:
            continue
        for win_name, methods in info["windows"].items():
            for method, metrics in methods.items():
                detail_rows.append({"horizon": horizon, "window": win_name, "method": method, **metrics})
    pd.DataFrame(detail_rows).to_csv(REPORTS_DIR / "hynix_ml_backtest_detail.csv", index=False, encoding="utf-8-sig")

    lines = ["# SK하이닉스 ML 앙상블 백테스트 리포트", "",
              f"생성일시: {datetime.now().isoformat(timespec='seconds')}", "",
              "예측 수익률을 보장하지 않으며, 통계적 참고 자료입니다.", "", "## Horizon별 성과(전체 테스트 구간, ensemble 기준)", "",
              "| horizon | 표본 | MAPE% | MAE | 방향적중률 | 기준 충족 |", "|---|---|---|---|---|---|"]
    for horizon in ALL_HORIZONS:
        info = summary["horizons"].get(horizon, {})
        if "error" in info or "windows" not in info:
            lines.append(f"| {horizon} | - | - | - | - | 데이터 부족: {info.get('error', 'no_data')} |")
            continue
        m = info["windows"]["full"]["ensemble"]
        meets = "OK" if _meets_bar(horizon, m) else "미달 -> Rule 중심 사용 권장"
        lines.append(f"| {horizon} | {m.get('n')} | {m.get('mape_pct')} | {m.get('mae')} | {m.get('direction_accuracy')} | {meets} |")

    lines += ["", "## Rule vs ML vs Ensemble 비교 (전체 구간)", "",
              "| horizon | method | MAPE% | 방향적중률 | UP precision/recall | DOWN precision/recall |", "|---|---|---|---|---|---|"]
    for horizon in ALL_HORIZONS:
        info = summary["horizons"].get(horizon, {})
        if "error" in info or "windows" not in info:
            continue
        for method in ("rule", "ml", "ensemble"):
            m = info["windows"]["full"][method]
            lines.append(
                f"| {horizon} | {method} | {m.get('mape_pct')} | {m.get('direction_accuracy')} | "
                f"{m.get('up_precision')}/{m.get('up_recall')} | {m.get('down_precision')}/{m.get('down_recall')} |"
            )

    lines += ["", "## 최근 3개월 vs 최근 1개월 vs 전체 (ensemble 기준)", "",
              "| horizon | 구간 | 표본 | MAPE% | 방향적중률 |", "|---|---|---|---|---|"]
    for horizon in ALL_HORIZONS:
        info = summary["horizons"].get(horizon, {})
        if "error" in info or "windows" not in info:
            continue
        for win_name in ("full", "recent_3m", "recent_1m"):
            m = info["windows"][win_name]["ensemble"]
            lines.append(f"| {horizon} | {win_name} | {m.get('n')} | {m.get('mape_pct')} | {m.get('direction_accuracy')} |")

    lines += ["", "## 합격 기준 참고", "",
              "- 30분 MAPE 0.8% 이하 / 1시간 1.2% / 3시간 1.8% / 종가 1.5% / 내일시가 2.0%, 방향적중률 60% 이상",
              "- 기준 미달 horizon은 UI에 \"ML 모델 신뢰도 낮음, Rule 중심 사용\"으로 표시된다."]

    (REPORTS_DIR / "hynix_ml_backtest_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    print("캐시에서 과거 데이터 로드...")
    historical_data = load_all_from_cache()

    daily = fb.build_daily_feature_table(historical_data)
    intraday = fb.build_intraday_feature_table(historical_data)

    horizon_results = {}
    for horizon in DAILY_HORIZONS:
        if daily["table"].empty:
            horizon_results[horizon] = {"horizon": horizon, "error": "일봉 feature 없음", "rows": []}
            continue
        print(f"[{horizon}] 백테스트 중...")
        horizon_results[horizon] = backtest_horizon(horizon, daily["table"])

    for horizon in INTRADAY_HORIZONS:
        if intraday["table"].empty:
            horizon_results[horizon] = {"horizon": horizon, "error": "분봉 feature 없음", "rows": []}
            continue
        print(f"[{horizon}] 백테스트 중...")
        horizon_results[horizon] = backtest_horizon(horizon, intraday["table"])

    summary = summarize_backtest(horizon_results)
    write_reports(summary)
    print("리포트 저장: reports/hynix_ml_backtest_summary.md, reports/hynix_ml_backtest_detail.csv")


if __name__ == "__main__":
    main()
