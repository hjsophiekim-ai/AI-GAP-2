"""
hynix_short_term_signal.py — SK하이닉스 단기 전고점 예측 모듈.

마이크론/NVDA·AMD·SOX/환율·KOSPI/외국인·기관 수급/뉴스 모멘텀을 종합해
0~100점 short_term_score와 지지선·목표가·도달확률·매매판단을 계산한다.

기존 `hynix_predictor.py` / `hynix_swing_flag.py`(가격 예측, 스윙 플래그)와는
독립된 병렬 모델이다 — 기존 모델은 수정하지 않는다.

이 모듈은 확정적 예측을 하지 않는다. 모든 결과에는 반드시
`disclaimer`(면책 문구)가 포함되며, "무조건 상승"/"확정" 같은 표현은 쓰지 않는다.
필수 데이터가 누락되면 가격 계산을 생략하고 `blocked=True` + `missing_data`만 반환한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

DISCLAIMER = "확률 기반 참고자료이며 투자판단은 사용자 책임입니다."


def predict_hynix_signal(market_data: dict) -> dict:
    """SK하이닉스 단기 전고점 예측 신호를 계산한다.

    Parameters
    ----------
    market_data : app.data_sources.auto_market_collector.collect_all() 반환 dict
        keys: mu, nvda, amd, index, domestic_index, hynix, hynix_minute,
              investor_flow, kospilab, news

    Returns
    -------
    dict — 필드는 모듈 docstring 및 계획서 참고.
    """
    from app.features.hynix_auto_features import build_auto_features

    auto_features = build_auto_features(market_data)
    micron_features = auto_features.get("micron_features", {})
    tech = auto_features.get("tech_indicators", {})

    hynix_data = market_data.get("hynix", {}) or {}
    minute_data = market_data.get("hynix_minute", {}) or {}
    index_data = market_data.get("index", {}) or {}
    domestic_index = market_data.get("domestic_index", {}) or {}
    amd_data = market_data.get("amd", {}) or {}
    nvda_data = market_data.get("nvda", {}) or {}
    investor_data = market_data.get("investor_flow", {}) or {}
    news_data = market_data.get("news", {}) or {}

    current_price = hynix_data.get("current_price")
    df_daily = hynix_data.get("df_daily")
    df_1min = minute_data.get("df_1min")

    mu_regular = micron_features.get("micron_regular_return")
    mu_afterhours = micron_features.get("micron_aftermarket_return")
    if mu_afterhours is None:
        mu_afterhours = micron_features.get("micron_premarket_return")

    # ── 마이크론 정규장/장외 등락률이 없어도(장 마감/데이터 지연 등) 더 이상
    # 전체 제안 생성을 차단하지 않는다 — Micron Proxy Prediction Engine의
    # effective_micron_score/synthetic_micron_score/micron_data_confidence로
    # 대체한다(SOX/Nasdaq futures proxy + 미국 반도체 basket + 한국 확인신호
    # 기반 추정치이며, 실제 CME 선물 체결가가 아니다).
    micron_proxy_result: Optional[dict] = None
    micron_regular_score_source = "REAL"
    micron_afterhours_score_source = "REAL"
    if mu_regular is None or mu_afterhours is None:
        try:
            from app.models.micron_proxy_prediction import compute_effective_micron_score_from_market_data

            micron_proxy_result = compute_effective_micron_score_from_market_data(market_data)
        except Exception:
            micron_proxy_result = None

    nvda_ret = nvda_data.get("regular_return")
    amd_ret = amd_data.get("regular_return")
    sox_ret = index_data.get("sox_return")
    usdkrw_change = index_data.get("usdkrw_change")
    kospi_ret = domestic_index.get("kospi_return")
    kospi200_ret = domestic_index.get("kospi200_return")

    foreign_net = investor_data.get("foreign_net_buy")
    institution_net = investor_data.get("institution_net_buy")

    # ── 필수 데이터 완전성 검증 ──────────────────────────────────────────────
    missing_data: list[str] = []
    if current_price is None:
        missing_data.append("SK하이닉스 현재가")
    if df_daily is None or len(df_daily) < 20:
        missing_data.append("SK하이닉스 일봉(최근 20거래일 이상)")
    if df_1min is None or (hasattr(df_1min, "empty") and df_1min.empty):
        missing_data.append("SK하이닉스 분봉(1/3/5분봉)")
    # 마이크론 정규장/장외 등락률은 더 이상 단독으로 제안 생성을 차단하지 않는다
    # (Micron Proxy 대체 계산으로 진행 — 위 micron_proxy_result 참고). 단, Proxy
    # 계산마저 완전히 실패한 경우(예외)에만 데이터 누락으로 기록한다.
    if mu_regular is None and mu_afterhours is None and micron_proxy_result is None:
        missing_data.append("마이크론 정규장/장외 등락률 및 Proxy 대체 계산 모두 실패")
    if nvda_ret is None and amd_ret is None and sox_ret is None:
        missing_data.append("NVDA/AMD/SOX 등락률")
    if kospi_ret is None and kospi200_ret is None:
        missing_data.append("KOSPI/KOSPI200 등락률")
    if usdkrw_change is None:
        missing_data.append("원/달러 환율 변화율")
    if foreign_net is None and institution_net is None:
        missing_data.append("외국인/기관 순매수")

    if missing_data:
        return {
            "short_term_score": None,
            "score_breakdown": {},
            "direction": None,
            "blocked": True,
            "block_reason": "필수 데이터 누락으로 예측을 생성하지 않았습니다.",
            "missing_data": missing_data,
            "disclaimer": DISCLAIMER,
            "computed_at": datetime.now().isoformat(),
        }

    news_score = news_data.get("score", 5.0)
    news_ok = bool(news_data.get("success", False))

    # ── 점수 계산 (100점 만점) ───────────────────────────────────────────────
    if mu_regular is not None:
        micron_regular_score, _ = _linear_score(mu_regular, span=3.0, max_points=20.0)
    elif micron_proxy_result is not None:
        # effective_micron_score는 0~100(중립 50) 스케일 — max_points=20 스케일로 선형 환산.
        micron_regular_score = (micron_proxy_result.get("effective_micron_score", 50.0) / 100.0) * 20.0
        micron_regular_score_source = str(micron_proxy_result.get("micron_score_source", "PROXY"))
    else:
        micron_regular_score, _ = _linear_score(None, span=3.0, max_points=20.0)
        micron_regular_score_source = "NEUTRAL_FALLBACK"

    if mu_afterhours is not None:
        micron_afterhours_score, _ = _linear_score(mu_afterhours, span=3.0, max_points=10.0)
    elif micron_proxy_result is not None:
        micron_afterhours_score = (micron_proxy_result.get("effective_micron_score", 50.0) / 100.0) * 10.0
        micron_afterhours_score_source = str(micron_proxy_result.get("micron_score_source", "PROXY"))
    else:
        micron_afterhours_score, _ = _linear_score(None, span=3.0, max_points=10.0)
        micron_afterhours_score_source = "NEUTRAL_FALLBACK"

    us_semi_values = [v for v in (nvda_ret, amd_ret, sox_ret) if v is not None]
    us_semi_avg = sum(us_semi_values) / len(us_semi_values) if us_semi_values else None
    nvda_amd_sox_score, _ = _linear_score(us_semi_avg, span=3.0, max_points=15.0)

    minute_trend_pct = _minute_trend_pct(df_1min)
    hynix_minute_score, _ = _linear_score(minute_trend_pct, span=1.0, max_points=15.0)

    hynix_daily_score = _daily_position_score(tech, kospi_ret, current_price, df_daily)

    investor_flow_score = _investor_flow_score(foreign_net, institution_net, usdkrw_change)

    news_momentum_score = max(0.0, min(10.0, float(news_score)))

    score_breakdown = {
        "micron_regular": round(micron_regular_score, 2),
        "micron_afterhours": round(micron_afterhours_score, 2),
        "nvda_amd_sox": round(nvda_amd_sox_score, 2),
        "hynix_minute_trend": round(hynix_minute_score, 2),
        "hynix_daily_position": round(hynix_daily_score, 2),
        "investor_flow": round(investor_flow_score, 2),
        "news_momentum": round(news_momentum_score, 2),
    }
    short_term_score = round(sum(score_breakdown.values()), 2)

    if short_term_score >= 60:
        direction = "상승 우세"
    elif short_term_score <= 40:
        direction = "하락 우세"
    else:
        direction = "중립"

    # ── 지지선/목표가 ────────────────────────────────────────────────────────
    recent_high, recent_low = _recent_high_low(df_daily, window=20)
    rebound_rate = (current_price / recent_low - 1.0) if recent_low else None
    drawdown_rate = (current_price / recent_high - 1.0) if recent_high else None

    support_1 = _support_1(df_daily, df_1min, current_price)
    support_2 = _support_2(df_daily, recent_high)
    support_3 = recent_low

    target_1 = _target_1(df_1min, minute_trend_pct, recent_low, current_price)
    target_2 = recent_low + (recent_high - recent_low) * 0.618 if (recent_high and recent_low) else None
    target_3 = recent_high

    volume_change_pct = tech.get("volume_change_pct")
    volume_confirmed = not (volume_change_pct is not None and volume_change_pct < 0)

    target_1_probability = _target_probability(
        short_term_score, base_ratio=0.90, mu_afterhours=mu_afterhours,
        foreign_net=foreign_net, volume_confirmed=volume_confirmed,
    )
    target_2_probability = _target_probability(
        short_term_score, base_ratio=0.60, mu_afterhours=mu_afterhours,
        foreign_net=foreign_net, volume_confirmed=volume_confirmed,
    )
    target_3_probability = _target_probability(
        short_term_score, base_ratio=0.35, mu_afterhours=mu_afterhours,
        foreign_net=foreign_net, volume_confirmed=volume_confirmed,
    )
    # 목표가가 멀수록 확률이 낮아지도록 단조성 보장
    target_2_probability = min(target_2_probability, target_1_probability)
    target_3_probability = min(target_3_probability, target_2_probability)

    upper_wick_near_high = _upper_wick_near_high(df_1min, recent_high, current_price)

    prev_close = hynix_data.get("prev_close")
    hynix_today_return_pct = (
        (current_price / prev_close - 1.0) * 100 if (prev_close and prev_close > 0) else None
    )
    minute_last_bar_time = market_data.get("hynix_minute", {}).get("last_bar_time")
    raw_inputs = {
        "mu_regular_return": mu_regular,
        "mu_afterhours_return": mu_afterhours,
        "nvda_return": nvda_ret,
        "amd_return": amd_ret,
        "sox_return": sox_ret,
        "kospi_return": kospi_ret,
        "kospi200_return": kospi200_ret,
        "usdkrw_change": usdkrw_change,
        "foreign_net_buy": foreign_net,
        "institution_net_buy": institution_net,
        "hynix_prev_close": prev_close,
        "hynix_current_price": current_price,
        "hynix_today_return_pct": round(hynix_today_return_pct, 2) if hynix_today_return_pct is not None else None,
        "current_price_sources": hynix_data.get("current_price_sources"),
        "minute_last_bar_time": minute_last_bar_time,
    }

    judgement = _judgement(
        current_price=current_price, target_1=target_1, support_1=support_1,
        recent_high=recent_high, score=short_term_score,
        drawdown_rate=drawdown_rate, news_ok=news_ok,
    )

    reasons_top5 = _reasons_top5(
        mu_regular=mu_regular, mu_afterhours=mu_afterhours, us_semi_avg=us_semi_avg,
        minute_trend_pct=minute_trend_pct, tech=tech, foreign_net=foreign_net,
        institution_net=institution_net, news_score=news_score, news_ok=news_ok,
        kospi_ret=kospi_ret, usdkrw_change=usdkrw_change,
    )

    return {
        "short_term_score": short_term_score,
        "score_breakdown": score_breakdown,
        "direction": direction,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "rebound_rate": round(rebound_rate * 100, 2) if rebound_rate is not None else None,
        "drawdown_rate": round(drawdown_rate * 100, 2) if drawdown_rate is not None else None,
        "support_1": support_1,
        "support_2": support_2,
        "support_3": support_3,
        "target_1": target_1,
        "target_2": round(target_2, 0) if target_2 is not None else None,
        "target_3": target_3,
        "target_1_probability": round(target_1_probability, 1),
        "target_2_probability": round(target_2_probability, 1),
        "target_3_probability": round(target_3_probability, 1),
        "support_levels": [support_1, support_2, support_3],
        "target_levels": [target_1, target_2, target_3],
        "target_probabilities": {
            "target_1": round(target_1_probability, 1),
            "target_2": round(target_2_probability, 1),
            "target_3": round(target_3_probability, 1),
        },
        "volume_confirmed": volume_confirmed,
        "upper_wick_near_high": upper_wick_near_high,
        "judgement": judgement,
        "reasons_top5": reasons_top5,
        "missing_data": [],
        "news_warning": None if news_ok else "뉴스 데이터 수집 실패 - 중립값(5/10) 사용",
        "raw_inputs": raw_inputs,
        "micron_regular_score_source": micron_regular_score_source,
        "micron_afterhours_score_source": micron_afterhours_score_source,
        "micron_proxy_effective_score": micron_proxy_result.get("effective_micron_score") if micron_proxy_result else None,
        "micron_proxy_synthetic_score": micron_proxy_result.get("synthetic_micron_score") if micron_proxy_result else None,
        "micron_proxy_data_confidence": micron_proxy_result.get("micron_data_confidence") if micron_proxy_result else None,
        "blocked": False,
        "block_reason": None,
        "disclaimer": DISCLAIMER,
        "computed_at": datetime.now().isoformat(),
    }


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _linear_score(value: Optional[float], span: float, max_points: float) -> tuple[float, bool]:
    """value(%)를 [-span, span] 구간에서 [0, max_points]로 선형 매핑. 중립=max_points/2."""
    if value is None:
        return max_points / 2.0, False
    v = max(-span, min(span, float(value)))
    frac = (v + span) / (2.0 * span)
    return frac * max_points, True


def _recent_high_low(df_daily: pd.DataFrame, window: int = 20) -> tuple[Optional[float], Optional[float]]:
    if df_daily is None or len(df_daily) < window:
        return None, None
    tail = df_daily.sort_values("datetime").tail(window)
    return float(tail["high"].max()), float(tail["low"].min())


def _support_1(df_daily: pd.DataFrame, df_1min, current_price: Optional[float]) -> Optional[float]:
    try:
        if df_1min is not None and not df_1min.empty:
            from app.strategy.intraday_indicators import calculate_vwap

            candles = df_1min.to_dict("records")
            vwap = calculate_vwap(candles)
            if vwap:
                return round(vwap, 0)
    except Exception:
        pass
    if df_daily is not None and len(df_daily) >= 5:
        ma5 = float(df_daily.sort_values("datetime")["close"].tail(5).mean())
        return round(ma5, 0)
    return current_price


def _support_2(df_daily: pd.DataFrame, recent_high: Optional[float]) -> Optional[float]:
    """최근 고점 형성 이후 발생한 눌림목 저점."""
    if df_daily is None or recent_high is None or len(df_daily) < 5:
        return None
    tail = df_daily.sort_values("datetime").tail(20).reset_index(drop=True)
    high_idx = tail["high"].idxmax()
    after_peak = tail.iloc[high_idx:]
    if len(after_peak) < 2:
        after_peak = tail.tail(5)
    return round(float(after_peak["low"].min()), 0)


def _minute_trend_pct(df_1min) -> Optional[float]:
    if df_1min is None or df_1min.empty or len(df_1min) < 2:
        return None
    work = df_1min.sort_values("datetime")
    window = work.tail(min(30, len(work)))
    first_open = float(window.iloc[0]["open"])
    last_close = float(window.iloc[-1]["close"])
    if first_open <= 0:
        return None
    return (last_close / first_open - 1.0) * 100


def _target_1(df_1min, minute_trend_pct: Optional[float], recent_low: Optional[float], current_price: Optional[float]) -> Optional[float]:
    if df_1min is not None and not df_1min.empty:
        work = df_1min.sort_values("datetime")
        window = work.tail(min(60, len(work)))
        return round(float(window["high"].max()), 0)
    if recent_low and current_price:
        return round(max(current_price, recent_low), 0)
    return current_price


def _daily_position_score(tech: dict, kospi_ret: Optional[float], current_price, df_daily) -> float:
    """일봉 위치(RSI/이평선/20일 고저 대비) + KOSPI 상대강도 보정. 만점 15점."""
    rsi = tech.get("rsi_14")
    ma20_pos = tech.get("ma20_position_pct")
    from_high = tech.get("from_20d_high_pct")

    parts = []
    if rsi is not None:
        parts.append(max(0.0, min(1.0, rsi / 100.0)))
    if ma20_pos is not None:
        score, _ = _linear_score(ma20_pos, span=10.0, max_points=1.0)
        parts.append(score)
    if from_high is not None:
        score, _ = _linear_score(from_high, span=15.0, max_points=1.0)
        parts.append(score)

    base_frac = sum(parts) / len(parts) if parts else 0.5
    base = base_frac * 15.0

    # KOSPI 대비 상대강도 보정 (하이닉스 3일 수익률 - KOSPI 등락률)
    return_3d = tech.get("return_3d_pct")
    if return_3d is not None and kospi_ret is not None:
        relative_strength = return_3d - kospi_ret
        adj = max(-1.5, min(1.5, relative_strength * 0.15))
        base = max(0.0, min(15.0, base + adj))
    return base


def _investor_flow_score(foreign_net: Optional[float], institution_net: Optional[float], usdkrw_change: Optional[float]) -> float:
    """외국인/기관 순매수 + 환율 보정. 만점 15점."""
    net = 0.0
    count = 0
    if foreign_net is not None:
        net += foreign_net
        count += 1
    if institution_net is not None:
        net += institution_net
        count += 1
    if count == 0:
        base = 7.5
    else:
        # 순매수 수량 기준 대략적 스케일(수십만주 단위)로 정규화
        score, _ = _linear_score(net / 100_000.0, span=5.0, max_points=15.0)
        base = score

    if usdkrw_change is not None and usdkrw_change > 0:
        # 원화 약세 → 외국인 매수 유인 감소로 소폭 감점
        penalty = min(1.5, usdkrw_change * 0.5)
        base = max(0.0, base - penalty)
    return base


def _target_probability(
    score: float, base_ratio: float, mu_afterhours: Optional[float],
    foreign_net: Optional[float], volume_confirmed: bool,
) -> float:
    prob = score * base_ratio
    if mu_afterhours is not None and mu_afterhours < 0:
        prob -= 10.0
    if foreign_net is not None and foreign_net > 0:
        prob += 5.0
    if not volume_confirmed:
        prob -= 10.0
    return max(0.0, min(100.0, prob))


def _upper_wick_near_high(df_1min, recent_high: Optional[float], current_price: Optional[float]) -> bool:
    if df_1min is None or df_1min.empty or recent_high is None or current_price is None:
        return False
    if current_price < recent_high * 0.98:
        return False
    work = df_1min.sort_values("datetime").tail(10)
    if work.empty:
        return False
    last = work.iloc[-1]
    body_high = max(float(last["open"]), float(last["close"]))
    wick = float(last["high"]) - body_high
    body = abs(float(last["close"]) - float(last["open"])) or 1.0
    return wick > body * 1.2


def _judgement(
    current_price: Optional[float], target_1: Optional[float], support_1: Optional[float],
    recent_high: Optional[float], score: float, drawdown_rate: Optional[float], news_ok: bool,
) -> str:
    if current_price is None:
        return "관망"

    if target_1 and abs(current_price / target_1 - 1.0) <= 0.015:
        return "일부 익절 구간"
    if support_1 and current_price >= support_1 and score >= 65:
        return "눌림 시 매수 가능"
    if recent_high and current_price >= recent_high * 0.98 and score < 70:
        return "추격매수 금지"
    if drawdown_rate is not None and drawdown_rate * 100 <= -30 and news_ok:
        return "공포매수 후보"
    if drawdown_rate is not None and drawdown_rate * 100 <= -20 and score >= 55:
        return "분할매수 가능"
    if score < 50:
        return "반등 실패 위험"
    return "관망"


def _reasons_top5(
    mu_regular, mu_afterhours, us_semi_avg, minute_trend_pct, tech,
    foreign_net, institution_net, news_score, news_ok, kospi_ret, usdkrw_change,
) -> list[str]:
    candidates: list[tuple[float, str]] = []
    if mu_regular is not None:
        candidates.append((abs(mu_regular), f"마이크론 정규장 등락률 {mu_regular:+.2f}%"))
    if mu_afterhours is not None:
        candidates.append((abs(mu_afterhours), f"마이크론 장외 등락률 {mu_afterhours:+.2f}%"))
    if us_semi_avg is not None:
        candidates.append((abs(us_semi_avg), f"NVDA/AMD/SOX 평균 등락률 {us_semi_avg:+.2f}%"))
    if minute_trend_pct is not None:
        candidates.append((abs(minute_trend_pct), f"SK하이닉스 최근 분봉 추세 {minute_trend_pct:+.2f}%"))
    rsi = tech.get("rsi_14")
    if rsi is not None:
        candidates.append((abs(rsi - 50), f"RSI(14) {rsi:.1f}"))
    if foreign_net is not None:
        candidates.append((abs(foreign_net) / 10000.0, f"외국인 순매수 {foreign_net:+,.0f}주"))
    if institution_net is not None:
        candidates.append((abs(institution_net) / 10000.0, f"기관 순매수 {institution_net:+,.0f}주"))
    if kospi_ret is not None:
        candidates.append((abs(kospi_ret) * 0.5, f"KOSPI 등락률 {kospi_ret:+.2f}%"))
    if usdkrw_change is not None:
        candidates.append((abs(usdkrw_change) * 0.5, f"원/달러 환율 변화율 {usdkrw_change:+.2f}%"))
    candidates.append((
        abs(news_score - 5.0) if news_ok else 0.1,
        f"뉴스 모멘텀 점수 {news_score:.1f}/10" + ("" if news_ok else " (수집 실패, 중립값)"),
    ))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in candidates[:5]]
