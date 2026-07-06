"""feature_builder.py — historical_data_loader.collect_all_historical() 결과에서
ML 학습용 feature/target 테이블을 만든다.

두 가지 해상도의 테이블을 만든다(현실적 데이터 제약 때문 — 모듈 docstring
참고: historical_data_loader.py 상단 설명):

  build_daily_feature_table()   : 일봉 해상도, 1년 전체 사용 가능.
                                   close/next_open 타깃용.
  build_intraday_feature_table(): 하이닉스 최근 분봉 해상도(며칠~수십일).
                                   30m/1h/3h 타깃용. 국내/해외 컨텍스트
                                   feature는 "그날의 일봉 기준값"을 그대로
                                   붙인다(분봉 단위로 완벽히 동기화된 해외
                                   분봉 히스토리가 없기 때문 — 근사임을 명시).

lookahead 방지 원칙:
  - 모든 feature는 해당 시점 이전(≤) 데이터만 사용한다.
  - target은 해당 시점 이후(>) 데이터로만 만든다.
  - next_open(내일 시가) 타깃을 만들 때, "오늘 장마감 이후에만 알 수 있는
    정보"(오늘 종가, 오늘 최종 수급 등)는 next_open의 feature로는 써도
    되지만, 오늘 장중 시점 행(30m/1h/3h용 행)의 feature로는 쓰면 안 된다 —
    이 파일은 daily/intraday 테이블을 분리해 이 문제를 원천적으로 피한다
    (intraday 테이블의 feature는 해당 분봉 시점까지의 정보만 사용).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

SIDEWAYS_BAND_PCT = {"30m": 0.3, "1h": 0.5, "3h": 0.8, "close": 1.0, "next_open": 1.2}
HORIZONS = ("30m", "1h", "3h", "close", "next_open")


# ---------------------------------------------------------------------------
# 벡터화된 기술적 지표 (미래 데이터 사용 없음 — 전부 과거 window만 참조)
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    return ema12 - ema26


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def _zscore(series: pd.Series, window: int = 20) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0, np.nan)


def _vwap_from_ohlcv(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tv = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return cum_tv / cum_vol


def _direction(return_pct: float, band: float) -> str:
    if pd.isna(return_pct):
        return None
    if return_pct > band:
        return "UP"
    if return_pct < -band:
        return "DOWN"
    return "SIDEWAYS"


# ---------------------------------------------------------------------------
# 일봉 해상도 테이블 (close/next_open 타깃)
# ---------------------------------------------------------------------------

def _daily_return_features(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """단일 종목/지수 일봉 df(datetime, close[, open/high/low/volume]) -> 등락률 feature."""
    out = pd.DataFrame({"datetime": pd.to_datetime(df["datetime"]).dt.normalize()})
    close = df["close"].astype(float)
    out[f"{prefix}_return"] = close.pct_change() * 100
    out[f"{prefix}_return_3d"] = close.pct_change(3) * 100
    out[f"{prefix}_return_5d"] = close.pct_change(5) * 100
    if "volume" in df.columns:
        vol = df["volume"].astype(float)
        out[f"{prefix}_volume_zscore"] = _zscore(vol)
    if {"open", "high", "low", "volume"}.issubset(df.columns):
        vwap = _vwap_from_ohlcv(df)
        out[f"{prefix}_vwap_position"] = (close - vwap) / vwap.replace(0, np.nan) * 100
    return out


def build_daily_feature_table(historical_data: dict) -> dict:
    """
    Returns
    -------
    dict: {"table": DataFrame, "feature_columns": list, "warnings": list}
    """
    warnings: list[str] = []
    hynix = historical_data.get("hynix", {}).get("df")
    if hynix is None or hynix.empty:
        return {"table": pd.DataFrame(), "feature_columns": [], "warnings": ["하이닉스 일봉 데이터 없음 — 테이블 생성 불가"]}

    hynix = hynix.copy()
    hynix["datetime"] = pd.to_datetime(hynix["datetime"]).dt.normalize()
    base = pd.DataFrame({"datetime": hynix["datetime"]})

    close = hynix["close"].astype(float)
    base["hynix_close"] = close
    base["hynix_return_1d"] = close.pct_change() * 100
    base["hynix_return_3d"] = close.pct_change(3) * 100
    base["hynix_return_5d"] = close.pct_change(5) * 100
    base["hynix_rsi"] = _rsi(close)
    base["hynix_macd"] = _macd(close)
    base["hynix_obv"] = _obv(close, hynix["volume"].astype(float))
    base["hynix_volume_zscore"] = _zscore(hynix["volume"].astype(float))
    vwap = _vwap_from_ohlcv(hynix)
    base["hynix_vwap_position"] = (close - vwap) / vwap.replace(0, np.nan) * 100
    day_high, day_low, day_open = hynix["high"].astype(float), hynix["low"].astype(float), hynix["open"].astype(float)
    rng = (day_high - day_low).replace(0, np.nan)
    base["hynix_close_location_in_range"] = (close - day_low) / rng
    base["hynix_intraday_high_position"] = (day_high - close) / rng
    base["hynix_intraday_low_recovery"] = (close - day_low) / rng

    def _merge_context(key: str, prefix: str, is_index: bool = False):
        nonlocal base
        node = historical_data.get(key, {})
        df = node.get("df")
        if df is None or df.empty:
            warnings.append(f"{prefix} 데이터 없음 — feature 제외")
            return
        feats = _daily_return_features(df, prefix)
        base = base.merge(feats, on="datetime", how="left")

    _merge_context("samsung", "samsung")
    _merge_context("hanmi", "hanmi")
    _merge_context("kospi", "kospi", is_index=True)
    _merge_context("kosdaq", "kosdaq", is_index=True)
    _merge_context("kospi200", "kospi200_futures", is_index=True)
    _merge_context("usdkrw", "usdkrw")
    _merge_context("mu", "mu")
    _merge_context("nvda", "nvda")
    _merge_context("amd", "amd")
    _merge_context("avgo", "avgo")
    _merge_context("qqq", "qqq")
    _merge_context("sox_proxy", "soxx_or_smh")

    if "mu_return" in base.columns and "soxx_or_smh_return" in base.columns:
        base["mu_relative_strength_vs_sox"] = base["mu_return"] - base["soxx_or_smh_return"]
    if "mu_return" in base.columns and "qqq_return" in base.columns:
        base["mu_relative_strength_vs_qqq"] = base["mu_return"] - base["qqq_return"]

    # ── 타깃 (미래 시프트 — lookahead 없음: shift(-1)은 "다음 행"이므로 미래값을
    #    별도 target 컬럼에만 넣고, feature 컬럼에는 절대 들어가지 않는다) ──────
    base["target_return_close"] = close.pct_change().shift(-1) * 100  # 다음 거래일 종가 대비 = "오늘 판단 -> 다음날 종가"
    next_open = day_open.shift(-1)
    base["target_return_next_open"] = (next_open - close) / close * 100

    base["target_direction_close"] = base["target_return_close"].apply(lambda v: _direction(v, SIDEWAYS_BAND_PCT["close"]))
    base["target_direction_next_open"] = base["target_return_next_open"].apply(lambda v: _direction(v, SIDEWAYS_BAND_PCT["next_open"]))

    feature_columns = [c for c in base.columns if c not in (
        "datetime", "hynix_close",
        "target_return_close", "target_return_next_open",
        "target_direction_close", "target_direction_next_open",
    )]

    return {"table": base, "feature_columns": feature_columns, "warnings": warnings}


# ---------------------------------------------------------------------------
# 분봉 해상도 테이블 (30m/1h/3h 타깃) — 하이닉스 최근 분봉 기준
# ---------------------------------------------------------------------------

def build_intraday_feature_table(historical_data: dict, bar_minutes: int = 5) -> dict:
    """
    하이닉스 최근 분봉(historical_data["hynix_intraday"]["df"])으로 30분/1시간/
    3시간 타깃용 feature 테이블을 만든다. 국내/해외 시장 컨텍스트는 "그 날의
    일봉 기준값"을 그대로 붙인다(분봉 단위 완전동기화 히스토리가 없어 근사).
    """
    warnings: list[str] = []
    intraday_node = historical_data.get("hynix_intraday", {})
    df = intraday_node.get("df")
    if df is None or df.empty or intraday_node.get("granularity") in (None, "none", "daily"):
        return {
            "table": pd.DataFrame(), "feature_columns": [],
            "warnings": ["하이닉스 분봉 데이터 없음(또는 일봉으로 대체됨) — 30m/1h/3h 학습 불가, Rule 예측에 의존 필요"],
        }

    df = df.copy()
    if "datetime" not in df.columns:
        return {"table": pd.DataFrame(), "feature_columns": [], "warnings": ["분봉 데이터에 datetime 컬럼 없음"]}
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = np.nan
    df = df.sort_values("datetime").reset_index(drop=True)

    step = pd.Timedelta(minutes=bar_minutes)
    inferred = df["datetime"].diff().median()
    bars_per_step = max(1, int(round(step / inferred))) if pd.notna(inferred) and inferred > pd.Timedelta(0) else 1

    close = df["close"].astype(float)
    df["return_1m"] = close.pct_change() * 100
    df["return_5m"] = close.pct_change(max(1, round(5 / max(bar_minutes, 1)))) * 100
    df["return_15m"] = close.pct_change(max(1, round(15 / max(bar_minutes, 1)))) * 100
    df["return_30m"] = close.pct_change(max(1, round(30 / max(bar_minutes, 1)))) * 100
    df["return_60m"] = close.pct_change(max(1, round(60 / max(bar_minutes, 1)))) * 100
    df["rsi"] = _rsi(close)
    df["macd"] = _macd(close)
    df["obv"] = _obv(close, df["volume"].astype(float))
    df["volume_zscore"] = _zscore(df["volume"].astype(float))
    df["trading_value_zscore"] = _zscore((df["volume"].astype(float) * close))
    vwap = _vwap_from_ohlcv(df)
    df["vwap_position"] = (close - vwap) / vwap.replace(0, np.nan) * 100
    df["distance_from_vwap"] = close - vwap

    df["_date"] = df["datetime"].dt.normalize()
    day_high = df.groupby("_date")["high"].cummax()
    day_low = df.groupby("_date")["low"].cummin()
    day_range = (day_high - day_low).replace(0, np.nan)
    df["intraday_high_position"] = (day_high - close) / day_range
    df["intraday_low_recovery"] = (close - day_low) / day_range
    df["close_location_in_range"] = (close - day_low) / day_range

    # ── 그 날의 일봉 기준 시장 컨텍스트 feature 병합(근사) ──────────────────
    daily = build_daily_feature_table(historical_data)["table"]
    if not daily.empty:
        context_cols = [c for c in daily.columns if c not in ("datetime", "hynix_close") and not c.startswith("target_")]
        df = df.merge(daily[["datetime"] + context_cols].rename(columns={"datetime": "_date"}), on="_date", how="left")
    else:
        warnings.append("일봉 기반 시장 컨텍스트 feature 병합 불가(일봉 테이블 비어 있음)")

    bars_30m = max(1, round(30 / max(bar_minutes, 1)))
    bars_60m = max(1, round(60 / max(bar_minutes, 1)))
    bars_3h = max(1, round(180 / max(bar_minutes, 1)))

    def _future_return_within_day(n_bars: int) -> pd.Series:
        future_close = close.shift(-n_bars)
        future_date = df["_date"].shift(-n_bars)
        ret = (future_close - close) / close * 100
        return ret.where(future_date == df["_date"])  # 다음 거래일로 넘어가면 타깃 무효화(세션 경계 보호)

    df["target_return_30m"] = _future_return_within_day(bars_30m)
    df["target_return_1h"] = _future_return_within_day(bars_60m)
    df["target_return_3h"] = _future_return_within_day(bars_3h)
    for h in ("30m", "1h", "3h"):
        df[f"target_direction_{h}"] = df[f"target_return_{h}"].apply(lambda v: _direction(v, SIDEWAYS_BAND_PCT[h]))

    feature_columns = [c for c in df.columns if c not in (
        "datetime", "_date", "open", "high", "low", "close", "volume",
        "target_return_30m", "target_return_1h", "target_return_3h",
        "target_direction_30m", "target_direction_1h", "target_direction_3h",
    )]

    return {"table": df.drop(columns=["_date"]), "feature_columns": feature_columns, "warnings": warnings,
            "bars_per_5m": bars_per_step, "intraday_source": intraday_node.get("source"),
            "intraday_granularity": intraday_node.get("granularity")}
