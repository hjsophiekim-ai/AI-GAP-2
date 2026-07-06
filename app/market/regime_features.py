"""
regime_features.py

수집된 market snapshot으로부터 A~F 유형 판단에 사용할 6종 점수(0~100)와
판단 플래그를 계산한다. 모든 함수는 순수 함수이며 네트워크 접근이 없다.
"""

from __future__ import annotations

from typing import Optional


def _num(value, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _rate(node: Optional[dict], default: float = 0.0) -> float:
    if not node:
        return default
    v = node.get("change_rate")
    return _num(v, default)


def _norm(rate: float, scale: float, base: float = 50.0) -> float:
    """등락률(%) -> 0~100 점수. rate=0 -> base, rate=scale -> base+33.3 (clip)."""
    return max(0.0, min(100.0, base + (rate / scale) * 33.33))


def _count_us_semi_rebound(overseas: dict) -> int:
    """마이크론/SOX/엔비디아 중 양호(상승)한 개수. 실시간 실패 시 마지막거래일로 대체."""
    last_session = overseas.get("us_last_session", {}) or {}
    count = 0
    for key in ("micron", "sox", "nvidia"):
        node = overseas.get(key)
        if node and node.get("success"):
            rate = _rate(node)
        else:
            ls_node = last_session.get(key)
            rate = ls_node.get("change_rate") if ls_node and ls_node.get("success") else None
        if rate is not None and rate > 0:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 1. us_ai_score
# ---------------------------------------------------------------------------

def compute_us_ai_score(snapshot: dict) -> float:
    overseas = snapshot.get("overseas", {})
    weights = {
        "nasdaq": (0.30, 1.5),
        "sox": (0.25, 2.5),
        "micron": (0.20, 4.0),
        "nvidia": (0.15, 4.0),
        "amd": (0.05, 4.0),
        "broadcom": (0.05, 4.0),
    }
    total = 0.0
    weight_sum = 0.0
    for key, (w, scale) in weights.items():
        node = overseas.get(key)
        if not node or not node.get("success"):
            continue
        total += _norm(_rate(node), scale) * w
        weight_sum += w
    if weight_sum == 0:
        return 50.0
    return round(total / weight_sum, 2)


# ---------------------------------------------------------------------------
# 2. korea_open_score
# ---------------------------------------------------------------------------

def compute_korea_open_score(snapshot: dict, ref_0920: Optional[dict] = None) -> float:
    domestic = snapshot.get("domestic", {})
    kospi = domestic.get("kospi", {})
    kosdaq = domestic.get("kosdaq", {})

    gap_score = _norm(_rate(kospi), 1.0) * 0.6 + _norm(_rate(kosdaq), 1.5) * 0.4

    adv = domestic.get("advancers") or 0
    dec = domestic.get("decliners") or 0
    breadth_score = 50.0
    if (adv + dec) > 0:
        breadth_score = (adv / (adv + dec)) * 100.0

    hold_score = 50.0
    if ref_0920 and kospi.get("value") is not None:
        ref_kospi = ref_0920.get("kospi_value")
        if ref_kospi:
            change_since_0920 = (kospi["value"] - ref_kospi) / ref_kospi * 100
            hold_score = _norm(change_since_0920, 0.5)

    score = gap_score * 0.45 + breadth_score * 0.25 + hold_score * 0.30
    return round(max(0.0, min(100.0, score)), 2)


# ---------------------------------------------------------------------------
# 3. leader_sector_score
# ---------------------------------------------------------------------------

def compute_leader_sector_score(snapshot: dict) -> float:
    domestic = snapshot.get("domestic", {})
    sector_rates = domestic.get("sector_change_rates", {}) or {}
    theme_rates = domestic.get("theme_change_rates", {}) or {}

    if not sector_rates and not theme_rates:
        return 40.0

    sorted_sectors = sorted(sector_rates.values(), reverse=True)
    top5_avg = sum(sorted_sectors[:5]) / len(sorted_sectors[:5]) if sorted_sectors else 0.0
    top5_score = _norm(top5_avg, 3.0)

    sorted_themes = sorted(theme_rates.values(), reverse=True)
    top10_avg = sum(sorted_themes[:10]) / len(sorted_themes[:10]) if sorted_themes else top5_avg
    top10_score = _norm(top10_avg, 3.0)

    clarity_bonus = 0.0
    if len(sorted_sectors) >= 2 and sorted_sectors[0] > 0:
        gap = sorted_sectors[0] - sorted_sectors[1]
        clarity_bonus = max(0.0, min(15.0, gap * 3))

    tv_top50 = domestic.get("trading_value_top50", []) or []
    concentration_bonus = 0.0
    if tv_top50:
        total_tv = sum(s.get("trading_value", 0) for s in tv_top50)
        top_tv = sum(s.get("trading_value", 0) for s in tv_top50[:5])
        if total_tv > 0:
            concentration_bonus = min(10.0, (top_tv / total_tv) * 20)

    score = top5_score * 0.45 + top10_score * 0.35 + clarity_bonus + concentration_bonus
    return round(max(0.0, min(100.0, score)), 2)


# ---------------------------------------------------------------------------
# 4. semiconductor_rebound_score
# ---------------------------------------------------------------------------

def compute_semiconductor_rebound_score(snapshot: dict, ref_0920: Optional[dict] = None) -> float:
    domestic = snapshot.get("domestic", {})
    hynix = domestic.get("hynix", {})
    samsung = domestic.get("samsung", {})
    overseas = snapshot.get("overseas", {})

    decline_score = 0.0
    for stock in (hynix, samsung):
        d1 = stock.get("day1_return")
        d2 = stock.get("day2_cum_return")
        if isinstance(d1, (int, float)) and d1 < 0:
            decline_score += min(20.0, abs(d1) * 4)
        if isinstance(d2, (int, float)) and d2 < 0:
            decline_score += min(15.0, abs(d2) * 2)
    decline_score = min(35.0, decline_score)

    recovery_score = 0.0
    for stock, ref_key in ((hynix, "hynix_low"), (samsung, "samsung_low")):
        price = stock.get("current_price")
        ref_low = (ref_0920 or {}).get(ref_key)
        if price and ref_low and ref_low > 0:
            recov_pct = (price - ref_low) / ref_low * 100
            recovery_score += max(0.0, min(20.0, recov_pct * 8))
    recovery_score = min(35.0, recovery_score)

    us_rebound_count = _count_us_semi_rebound(overseas)
    us_score = {0: 0.0, 1: 10.0, 2: 22.0, 3: 30.0}.get(us_rebound_count, 0.0)

    score = decline_score + recovery_score + us_score
    return round(max(0.0, min(100.0, score)), 2)


# ---------------------------------------------------------------------------
# 5. risk_off_score
# ---------------------------------------------------------------------------

def compute_risk_off_score(snapshot: dict) -> float:
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})
    kospi_rate = _rate(domestic.get("kospi"))
    kosdaq_rate = _rate(domestic.get("kosdaq"))

    index_score = 0.0
    worst = min(kospi_rate, kosdaq_rate)
    if worst < 0:
        index_score = min(50.0, abs(worst) * 20)

    fx_score = 0.0
    fx_rate = _rate(overseas.get("usdkrw"))
    if fx_rate > 0:
        fx_score = min(20.0, fx_rate * 15)

    flow = domestic.get("investor_flow") or {}
    foreign_sell = _num(flow.get("foreign_net_buy", 0))
    flow_score = 15.0 if foreign_sell < 0 else 0.0

    hynix_rate = _rate(domestic.get("hynix"))
    samsung_rate = _rate(domestic.get("samsung"))
    large_cap_score = 0.0
    if hynix_rate < 0 and samsung_rate < 0:
        large_cap_score = min(15.0, (abs(hynix_rate) + abs(samsung_rate)) * 3)

    score = index_score + fx_score + flow_score + large_cap_score
    return round(max(0.0, min(100.0, score)), 2)


# ---------------------------------------------------------------------------
# 6. gap_failure_score
# ---------------------------------------------------------------------------

def compute_gap_failure_score(snapshot: dict, ref_0920: Optional[dict] = None) -> float:
    domestic = snapshot.get("domestic", {})
    hynix = domestic.get("hynix", {})
    samsung = domestic.get("samsung", {})

    score = 0.0
    for stock in (hynix, samsung):
        open_p = stock.get("open")
        current = stock.get("current_price")
        high = stock.get("high")
        prev_close = stock.get("prev_close")

        if open_p and prev_close and prev_close > 0:
            gap_rate = (open_p - prev_close) / prev_close * 100
            if gap_rate > 1.0 and current and current < open_p:
                broke_open_pct = (open_p - current) / open_p * 100
                score += min(30.0, broke_open_pct * 10)

        if high and open_p and current and open_p > 0:
            wick_ratio = (high - current) / open_p * 100
            if wick_ratio > 1.5:
                score += min(20.0, wick_ratio * 5)

    tv_top50 = domestic.get("trading_value_top50", []) or []
    kospi = domestic.get("kospi", {})
    if tv_top50 and kospi.get("change_rate") is not None and kospi["change_rate"] < 0.3:
        score += 10.0

    return round(max(0.0, min(100.0, score)), 2)


def compute_all_scores(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    return {
        "us_ai_score": compute_us_ai_score(snapshot),
        "korea_open_score": compute_korea_open_score(snapshot, ref_0920),
        "leader_sector_score": compute_leader_sector_score(snapshot),
        "semiconductor_rebound_score": compute_semiconductor_rebound_score(snapshot, ref_0920),
        "risk_off_score": compute_risk_off_score(snapshot),
        "gap_failure_score": compute_gap_failure_score(snapshot, ref_0920),
    }


# ---------------------------------------------------------------------------
# Flags (boolean gate conditions used by regime_rules)
# ---------------------------------------------------------------------------

def compute_flags(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})
    hynix = domestic.get("hynix", {})
    samsung = domestic.get("samsung", {})
    kospi_rate = _rate(domestic.get("kospi"))
    kosdaq_rate = _rate(domestic.get("kosdaq"))
    nasdaq_rate = _rate(overseas.get("nasdaq"))
    sox_rate = _rate(overseas.get("sox"))

    flags = {
        "us_bullish": nasdaq_rate > 0 and sox_rate > 0,
        "kospi_or_kosdaq_below_neg1_5": kospi_rate <= -1.5 or kosdaq_rate <= -1.5,
        "usdkrw_rising": _rate(overseas.get("usdkrw")) > 0.3,
        "hynix_samsung_weak": _rate(hynix) < -1.0 and _rate(samsung) < -1.0,
        "prior_decline": (
            isinstance(hynix.get("day1_return"), (int, float)) and hynix["day1_return"] < -1.5
        ) or (
            isinstance(samsung.get("day1_return"), (int, float)) and samsung["day1_return"] < -1.5
        ),
        "us_semi_rebound_2of3": _count_us_semi_rebound(overseas) >= 2,
        "korea_open_holds": True,
        "recovered_from_0920_low": False,
        "gap_up_then_broke_open": False,
        "upper_wick_large": False,
        "leader_sector_clear": compute_leader_sector_score(snapshot) >= 65,
    }

    if ref_0920:
        ref_kospi = ref_0920.get("kospi_value")
        kospi_value = domestic.get("kospi", {}).get("value")
        if ref_kospi and kospi_value:
            flags["korea_open_holds"] = kospi_value >= ref_kospi * 0.999

        for stock, ref_key in ((hynix, "hynix_low"), (samsung, "samsung_low")):
            price = stock.get("current_price")
            ref_low = ref_0920.get(ref_key)
            if price and ref_low and ref_low > 0 and price >= ref_low * 1.005:
                flags["recovered_from_0920_low"] = True

    for stock in (hynix, samsung):
        open_p, current, prev_close, high = (
            stock.get("open"), stock.get("current_price"),
            stock.get("prev_close"), stock.get("high"),
        )
        if open_p and prev_close and prev_close > 0 and current:
            gap_rate = (open_p - prev_close) / prev_close * 100
            if gap_rate > 1.0 and current < open_p:
                flags["gap_up_then_broke_open"] = True
        if high and open_p and current and open_p > 0:
            if (high - current) / open_p * 100 > 1.5:
                flags["upper_wick_large"] = True

    return flags


# ---------------------------------------------------------------------------
# 미국장 휴장/데이터 신선도/데이터 품질 (Holiday Mode 통합)
# ---------------------------------------------------------------------------

FRESH_SECONDS = 180
USABLE_SECONDS = 900


def is_holiday_mode(snapshot: dict) -> bool:
    """
    미국장 휴장/전일 휴장/주말로 인해 최근 미국 데이터에 공백이 있으면
    holiday_mode로 취급한다.

    주의: is_us_market_open은 이 시스템이 매일 실행되는 한국 장 시작 전
    (08:50~09:25 KST) 시점 기준으로는 거의 항상 False이므로 (미국 정규장이
    한국 밤 시간대이기 때문) holiday_mode 판단 기준으로 쓰지 않는다.
    """
    us_status = snapshot.get("overseas", {}).get("us_market_status", {}) or {}
    return bool(us_status.get("is_us_holiday")) or bool(us_status.get("is_us_weekend"))


def classify_data_gap_reason(snapshot: dict) -> str:
    """NORMAL / US_HOLIDAY / WEEKEND / EARLY_CLOSE / API_FAILURE / UNKNOWN.

    is_us_market_open은 판단 기준으로 쓰지 않는다 (is_holiday_mode 설명 참고).
    """
    overseas = snapshot.get("overseas", {})
    us_status = overseas.get("us_market_status", {}) or {}
    if not us_status:
        return "UNKNOWN"

    if us_status.get("is_us_holiday"):
        return "US_HOLIDAY"
    if us_status.get("is_us_weekend"):
        return "WEEKEND"
    if us_status.get("is_us_early_close"):
        return "EARLY_CLOSE"

    core_keys = ("micron", "nvidia", "amd", "broadcom", "sox", "nasdaq")
    any_success = any((overseas.get(k) or {}).get("success", False) for k in core_keys)
    return "NORMAL" if any_success else "API_FAILURE"


def compute_data_freshness_score(snapshot: dict) -> float:
    """
    실시간 데이터 신선도 점수(0~100).

    기준: 0~180초=신선(100점), 180~900초=사용가능(감점), 900초 초과=stale(0점).
    휴장일 마지막거래일 데이터(holiday_reference)는 stale로 취급하지 않고
    만점 처리한다.
    """
    overseas = snapshot.get("overseas", {})
    bars = overseas.get("us_realtime_bars", {}) or {}
    if not bars:
        # us_realtime_bars 자체가 없는 스냅샷(신선도 미추적)은 감점하지 않는다.
        return 100.0

    scores = []
    for key, node in bars.items():
        if not node.get("success"):
            scores.append(0.0)
            continue
        gap_reason = node.get("data_gap_reason", "NORMAL")
        if gap_reason == "MARKET_CLOSED":
            # 휴장/장외 시간대의 마지막가는 stale이 아니라 holiday_reference로 처리
            scores.append(100.0)
            continue
        freshness = node.get("freshness_seconds")
        if freshness is None:
            scores.append(70.0)
        elif freshness <= FRESH_SECONDS:
            scores.append(100.0)
        elif freshness <= USABLE_SECONDS:
            ratio = 1 - (freshness - FRESH_SECONDS) / (USABLE_SECONDS - FRESH_SECONDS)
            scores.append(max(30.0, 30.0 + 70.0 * ratio))
        else:
            scores.append(0.0)

    return round(sum(scores) / len(scores), 2) if scores else 50.0


def compute_data_quality_score(snapshot: dict) -> float:
    """
    전체 데이터 품질 점수(0~100) = 수집 성공비율(70%) + 신선도(30%).

    일반 개장일에 데이터가 오래되면(API_FAILURE) 크게 감점하고,
    미국 휴장으로 인한 공백(US_HOLIDAY/WEEKEND/EARLY_CLOSE)은 과도하게
    감점하지 않는다(holiday_adjusted 처리).
    """
    meta = snapshot.get("meta", {}) or {}
    base_ratio = float(meta.get("data_quality_ratio", 1.0)) * 100.0
    freshness_score = compute_data_freshness_score(snapshot)
    gap_reason = classify_data_gap_reason(snapshot)

    score = base_ratio * 0.70 + freshness_score * 0.30

    if gap_reason == "API_FAILURE":
        score *= 0.7  # 일반 개장일 데이터 오류 → 크게 감점
    elif gap_reason in ("US_HOLIDAY", "WEEKEND", "EARLY_CLOSE"):
        score = max(score, 75.0)  # 휴장으로 인한 공백은 과도하게 낮추지 않음

    return round(max(0.0, min(100.0, score)), 2)


def compute_holiday_adjusted_us_score(snapshot: dict) -> float:
    """
    Holiday Mode 전용 미국 지표 보정 점수(0~100).

    가중치(상대값, 100으로 정규화): 마지막거래일(MU/NVDA/SOX/NASDAQ) 25,
    선물/프리마켓 20, 환율/달러 15, 일본/대만 반도체 10 → 미국측 총 70/120.
    (KOSPI200 선물·외국인 수급 20 + 국내 09:20 흐름 30 은 korea_open_score/
    leader_sector_score 가중치 재조정으로 반영 — regime_rules.py 참고)
    """
    overseas = snapshot.get("overseas", {})
    last_session = overseas.get("us_last_session", {}) or {}
    holiday_inputs = overseas.get("holiday_mode_inputs", {}) or {}

    ls_scores = []
    for key in ("micron", "nvidia", "sox", "nasdaq"):
        node = last_session.get(key)
        if node and node.get("success") and node.get("change_rate") is not None:
            ls_scores.append(_norm(node["change_rate"], 3.0))
    last_session_score = sum(ls_scores) / len(ls_scores) if ls_scores else 50.0

    futures_scores = []
    us_futures = overseas.get("us_futures")
    if us_futures and us_futures.get("success"):
        futures_scores.append(_norm(_rate(us_futures), 1.5))
    nq_futures = holiday_inputs.get("nq_futures")
    if nq_futures and nq_futures.get("success") and nq_futures.get("change_rate") is not None:
        futures_scores.append(_norm(nq_futures["change_rate"], 1.5))
    futures_score = sum(futures_scores) / len(futures_scores) if futures_scores else 50.0

    fx_scores = []
    usdkrw = overseas.get("usdkrw")
    if usdkrw and usdkrw.get("success"):
        fx_scores.append(_norm(-_rate(usdkrw), 0.5))  # 환율 상승은 위험선호도에 부정적 → 부호 반전
    dxy = holiday_inputs.get("dxy")
    if dxy and dxy.get("success") and dxy.get("change_rate") is not None:
        fx_scores.append(_norm(-dxy["change_rate"], 0.5))
    fx_score = sum(fx_scores) / len(fx_scores) if fx_scores else 50.0

    jt_scores = []
    for key in ("japan_tokyo_electron", "japan_advantest", "japan_disco", "japan_screen", "taiwan_tsmc"):
        node = holiday_inputs.get(key)
        if node and node.get("success") and node.get("change_rate") is not None:
            jt_scores.append(_norm(node["change_rate"], 3.0))
    jt_score = sum(jt_scores) / len(jt_scores) if jt_scores else 50.0

    weights = {"last_session": 25.0, "futures": 20.0, "fx": 15.0, "jt": 10.0}
    total_weight = sum(weights.values())
    composite = (
        last_session_score * weights["last_session"]
        + futures_score * weights["futures"]
        + fx_score * weights["fx"]
        + jt_score * weights["jt"]
    ) / total_weight

    return round(max(0.0, min(100.0, composite)), 2)


# ---------------------------------------------------------------------------
# 실시간 장세 변화 감지용 예측 점수 (모두 0~100, 높을수록 "위험/악화" 방향)
#
# snapshot["deltas"]["5m"/"15m"] 는 market_data_collector.collect()가
# tick_history를 이용해 계산해 붙여준다. tick이 아직 부족하면(장 초반)
# 델타는 None이며, 아래 함수들은 이를 "중립(50점 근방)"으로 안전하게 처리한다.
# ---------------------------------------------------------------------------

def _share_flow_score(value: Optional[float], scale: float = 3_000_000.0) -> float:
    """수급 프록시(주식 수) -> 0~100. 순매도(음수)일수록 점수가 높아진다(위험)."""
    if value is None:
        return 50.0
    return max(0.0, min(100.0, 50.0 - (value / scale) * 33.33))


def _leader_sectors(snapshot: dict, top_n: int = 3) -> list:
    sector_rates = snapshot.get("domestic", {}).get("sector_change_rates", {}) or {}
    return [s for s, _ in sorted(sector_rates.items(), key=lambda x: x[1], reverse=True)[:top_n]]


def compute_leader_sector_rising_ratio(snapshot: dict) -> Optional[float]:
    """오늘 주도섹터 Top3 내 종목 중 상승(change_rate>0) 비율(0~100). 데이터 없으면 None."""
    domestic = snapshot.get("domestic", {})
    leaders = set(_leader_sectors(snapshot))
    tv_top50 = domestic.get("trading_value_top50", []) or []
    members = [s for s in tv_top50 if s.get("sector") in leaders]
    if not members:
        return None
    rising = sum(1 for s in members if _num(s.get("change_rate")) > 0)
    return round(rising / len(members) * 100, 1)


def compute_futures_pressure_score(snapshot: dict) -> float:
    """
    선물이 현물보다 먼저 약세 전환하는 매도압력 점수.

    구성: KOSPI200 선물 5분/15분 변화, 나스닥 선물 5분/15분 변화,
    선물이 현물(KOSPI)보다 먼저/더 크게 빠지는지 여부(보너스).
    """
    deltas = snapshot.get("deltas", {})
    d5, d15 = deltas.get("5m", {}) or {}, deltas.get("15m", {}) or {}

    weighted_sum = 0.0
    weight_total = 0.0
    for key, scale, w in (
        ("kospi200_futures_change_rate", 0.3, 30.0),
        ("nasdaq_futures_change_rate", 0.3, 25.0),
    ):
        v = d5.get(key)
        if v is not None:
            weighted_sum += _norm(-v, scale) * w
            weight_total += w
    for key, scale, w in (
        ("kospi200_futures_change_rate", 0.6, 20.0),
        ("nasdaq_futures_change_rate", 0.6, 15.0),
    ):
        v = d15.get(key)
        if v is not None:
            weighted_sum += _norm(-v, scale) * w
            weight_total += w

    if weight_total == 0:
        return 50.0
    score = weighted_sum / weight_total

    fut5 = d5.get("kospi200_futures_change_rate")
    spot5 = d5.get("kospi_change_rate")
    if fut5 is not None and spot5 is not None and fut5 < spot5 - 0.1:
        score = min(100.0, score + 10.0)  # 선물이 현물보다 먼저/더 빠짐 → 가산

    return round(max(0.0, min(100.0, score)), 2)


def compute_foreign_flow_reversal_score(snapshot: dict) -> float:
    """
    외국인 수급(프록시) 반전 위험 점수.

    실제 KOSPI200 선물 외국인 순매수 데이터는 무료로 안정 수집이 어려워
    하이닉스+삼성전자 개별종목 외국인 순매수 합계를 대리지표로 쓴다
    (market_data_collector._collect_market_investor_flow 참고).
    """
    domestic = snapshot.get("domestic", {})
    flow = domestic.get("investor_flow_market", {}) or {}
    if not flow.get("success"):
        return 50.0

    current = flow.get("foreign_net_buy_sum")
    score = _share_flow_score(current, scale=3_000_000.0)

    deltas = snapshot.get("deltas", {})
    d5 = (deltas.get("5m", {}) or {}).get("foreign_net_buy_proxy")
    d15 = (deltas.get("15m", {}) or {}).get("foreign_net_buy_proxy")
    if d5 is not None and d5 < 0:
        score += min(15.0, abs(d5) / 1_000_000 * 15.0)
    if d15 is not None and d15 < 0:
        score += min(10.0, abs(d15) / 2_000_000 * 10.0)

    return round(max(0.0, min(100.0, score)), 2)


def compute_fx_risk_score(snapshot: dict) -> float:
    """환율 급등(5분/15분) + 외국인 매도 동시 발생 시 가중되는 리스크 점수."""
    overseas = snapshot.get("overseas", {})
    domestic = snapshot.get("domestic", {})
    fx_rate = _rate(overseas.get("usdkrw"))
    score = _norm(fx_rate, 0.5)

    deltas = snapshot.get("deltas", {})
    d5 = (deltas.get("5m", {}) or {}).get("usdkrw_value")
    d15 = (deltas.get("15m", {}) or {}).get("usdkrw_value")
    if d5 is not None and d5 > 2.0:
        score += 15.0
    if d15 is not None and d15 > 4.0:
        score += 15.0

    flow = domestic.get("investor_flow_market", {}) or {}
    foreign = flow.get("foreign_net_buy_sum")
    if foreign is not None and foreign < 0 and fx_rate > 0.3:
        score += 10.0

    return round(max(0.0, min(100.0, score)), 2)


def compute_breadth_deterioration_score(snapshot: dict) -> float:
    """상승/하락 종목수 악화 + 주도섹터 내부 상승비율 악화."""
    domestic = snapshot.get("domestic", {})
    adv = _num(domestic.get("advancers"), 0.0)
    dec = _num(domestic.get("decliners"), 0.0)
    total = adv + dec

    score = 50.0
    if total > 0:
        breadth_ratio = adv / total * 100
        score = 100.0 - breadth_ratio

    deltas = snapshot.get("deltas", {})
    d5_adv = (deltas.get("5m", {}) or {}).get("advancers")
    d5_dec = (deltas.get("5m", {}) or {}).get("decliners")
    if d5_adv is not None and d5_adv < 0:
        score += min(15.0, abs(d5_adv) / 50.0)
    if d5_dec is not None and d5_dec > 0:
        score += min(15.0, d5_dec / 50.0)

    leader_ratio = compute_leader_sector_rising_ratio(snapshot)
    if leader_ratio is not None:
        score += (100.0 - leader_ratio) * 0.2

    return round(max(0.0, min(100.0, score)), 2)


def compute_semiconductor_leadership_score(snapshot: dict) -> float:
    """반도체 대장주(하이닉스/삼성전자/한미반도체)의 VWAP 대비 위치 강도(0~100, 높을수록 강세)."""
    domestic = snapshot.get("domestic", {})
    weighted = 0.0
    weight_total = 0.0
    for key, w in (("hynix", 40.0), ("samsung", 35.0), ("hanmi", 25.0)):
        stock = domestic.get(key, {}) or {}
        price = stock.get("current_price")
        vwap = stock.get("vwap")
        if price and vwap and vwap > 0:
            rel_pct = (price - vwap) / vwap * 100
            weighted += _norm(rel_pct, 1.0) * w
            weight_total += w
    if weight_total == 0:
        return 50.0
    return round(weighted / weight_total, 2)


def compute_theme_rotation_score(snapshot: dict, ref_0920: Optional[dict] = None) -> float:
    """
    09:20 주도섹터 대비 현재 주도섹터의 이탈(회전) 정도(0~100, 높을수록 많이 회전/붕괴).

    반도체/AI 주도에서 방산/전력/인버스 등 방어적 섹터로 이동한 경우 추가 가산.
    """
    current_leaders = _leader_sectors(snapshot)
    ref_leaders = (ref_0920 or {}).get("leader_sectors_0920") or []
    if not ref_leaders or not current_leaders:
        return 50.0

    overlap = len(set(current_leaders) & set(ref_leaders))
    retention = overlap / len(ref_leaders) * 100
    score = 100.0 - retention

    defensive_sectors = {"defense", "power_grid"}
    rotated_to_defensive = (
        "semiconductor" in ref_leaders
        and "semiconductor" not in current_leaders
        and any(s in defensive_sectors for s in current_leaders)
    )
    if rotated_to_defensive:
        score = min(100.0, score + 20.0)

    return round(max(0.0, min(100.0, score)), 2)


def compute_news_shock_score(snapshot: dict) -> float:
    """뉴스 모멘텀(부정 키워드) 점수를 0~100 충격 점수로 변환. 수집 실패 시 0(중립/unknown)."""
    news = snapshot.get("domestic", {}).get("news_shock", {}) or {}
    if not news.get("success"):
        return 0.0
    raw = news.get("score")
    if raw is None:
        return 0.0
    # raw: 0(매우 부정) ~ 5(중립) ~ 10(매우 긍정) — 중립 대비 부정 편차만 충격으로 환산
    shock = max(0.0, (5.0 - float(raw))) * 20.0
    return round(max(0.0, min(100.0, shock)), 2)


def compute_market_collapse_score(snapshot: dict, ref_0920: Optional[dict] = None) -> float:
    """시장 급락 종합 점수(0~100). 외국인 수급/선물압력/환율/breadth/반도체/테마회전 종합."""
    foreign = compute_foreign_flow_reversal_score(snapshot)
    futures = compute_futures_pressure_score(snapshot)
    fx = compute_fx_risk_score(snapshot)
    breadth = compute_breadth_deterioration_score(snapshot)
    semi_weak = 100.0 - compute_semiconductor_leadership_score(snapshot)
    rotation = compute_theme_rotation_score(snapshot, ref_0920)

    weights = {
        "foreign": 25.0, "futures": 20.0, "fx": 15.0,
        "breadth": 15.0, "semi_weak": 15.0, "rotation": 10.0,
    }
    total = sum(weights.values())
    composite = (
        foreign * weights["foreign"] + futures * weights["futures"] + fx * weights["fx"]
        + breadth * weights["breadth"] + semi_weak * weights["semi_weak"] + rotation * weights["rotation"]
    ) / total
    return round(max(0.0, min(100.0, composite)), 2)


def compute_semiconductor_collapse_score(snapshot: dict) -> float:
    """반도체 섹터 급락 종합 점수(0~100). VWAP 이탈/한미반도체 급락/섹터breadth/미국지표/수급 종합."""
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})

    semi_weak = 100.0 - compute_semiconductor_leadership_score(snapshot)

    hanmi_rate = _rate(domestic.get("hanmi"))
    hanmi_crash = _norm(-hanmi_rate, 3.0)

    leader_ratio = compute_leader_sector_rising_ratio(snapshot)
    semi_sector_rate = domestic.get("sector_change_rates", {}).get("semiconductor")
    if leader_ratio is not None and "semiconductor" in _leader_sectors(snapshot):
        sector_breadth_weak = 100.0 - leader_ratio
    elif semi_sector_rate is not None:
        sector_breadth_weak = _norm(-semi_sector_rate, 2.0)
    else:
        sector_breadth_weak = 50.0

    us_semi_weak_components = []
    for key, scale in (("micron", 4.0), ("nvidia", 4.0), ("sox", 2.5), ("nasdaq", 1.5)):
        node = overseas.get(key)
        if node and node.get("success"):
            us_semi_weak_components.append(_norm(-_rate(node), scale))
    us_semi_weak = sum(us_semi_weak_components) / len(us_semi_weak_components) if us_semi_weak_components else 50.0

    foreign = compute_foreign_flow_reversal_score(snapshot)

    weights = {"vwap": 35.0, "hanmi": 15.0, "breadth": 15.0, "us": 25.0, "foreign": 10.0}
    total = sum(weights.values())
    composite = (
        semi_weak * weights["vwap"] + hanmi_crash * weights["hanmi"] + sector_breadth_weak * weights["breadth"]
        + us_semi_weak * weights["us"] + foreign * weights["foreign"]
    ) / total
    return round(max(0.0, min(100.0, composite)), 2)


def compute_prediction_scores(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    """실시간 장세 변화 감지용 9종 점수를 한번에 계산한다."""
    return {
        "futures_pressure_score": compute_futures_pressure_score(snapshot),
        "foreign_flow_reversal_score": compute_foreign_flow_reversal_score(snapshot),
        "fx_risk_score": compute_fx_risk_score(snapshot),
        "breadth_deterioration_score": compute_breadth_deterioration_score(snapshot),
        "semiconductor_leadership_score": compute_semiconductor_leadership_score(snapshot),
        "theme_rotation_score": compute_theme_rotation_score(snapshot, ref_0920),
        "news_shock_score": compute_news_shock_score(snapshot),
        "market_collapse_score": compute_market_collapse_score(snapshot, ref_0920),
        "semiconductor_collapse_score": compute_semiconductor_collapse_score(snapshot),
    }

    return flags
