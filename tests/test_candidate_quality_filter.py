"""
tests/test_candidate_quality_filter.py

CandidateQualityFilter 유닛 테스트.
외부 API 호출 없이 mock 데이터로만 동작한다.
"""

import pytest
from app.models import Candidate, StockData
from app.strategy.candidate_quality_filter import CandidateQualityFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    symbol="005930",
    name="삼성전자",
    current_price=70000.0,
    open_p=68000.0,
    high=71000.0,
    low=67500.0,
    previous_close=66000.0,
    gap_rate=3.0,
    trade_value=50_000_000_000.0,
    rule_score=50.0,
) -> Candidate:
    return Candidate(
        rank=1,
        symbol=symbol,
        name=name,
        current_price=current_price,
        open=open_p,
        high=high,
        low=low,
        previous_close=previous_close,
        gap_rate=gap_rate,
        open_to_current_rate=(current_price - open_p) / open_p * 100,
        trade_value=trade_value,
        rule_score=rule_score,
        final_score=rule_score,
    )


def _make_stock_data(
    symbol="005930",
    name="삼성전자",
    is_etf=False,
    is_etn=False,
    is_preferred=False,
    is_spac=False,
    is_reit=False,
    is_warning=False,
    is_halt=False,
    current_price=70000.0,
    trade_value=50_000_000_000.0,
) -> StockData:
    return StockData(
        symbol=symbol,
        name=name,
        is_etf=is_etf,
        is_etn=is_etn,
        is_preferred=is_preferred,
        is_spac=is_spac,
        is_reit=is_reit,
        is_warning=is_warning,
        is_halt=is_halt,
        current_price=current_price,
        trade_value=trade_value,
    )


def _make_daily(n=25, base=65000.0, trend=0.0) -> list[dict]:
    """최신 순 n일 일봉 데이터. trend: 일당 등락 (양수=상승)."""
    result = []
    price = base
    for i in range(n):
        result.append({
            "date": f"202606{20 - i:02d}",
            "close": round(price, 0),
            "open": round(price * 0.99, 0),
            "high": round(price * 1.01, 0),
            "low": round(price * 0.98, 0),
            "volume": 1_000_000,
        })
        price -= trend  # 최신→과거 순이므로 순서 반전 효과
    return result


def _default_filter(overrides: dict = None) -> CandidateQualityFilter:
    """테스트용 설정을 가진 필터 인스턴스."""
    from app.config import get_config, reload_config
    cfg = get_config()
    base_qcfg = {
        "enabled": True,
        "speed_mode": False,
        "max_candidates_for_heavy_filters": 100,
        "min_price": 1000,
        "min_trading_value_0920": 3_000_000_000,
        "max_open_gap_rate": 12.0,
        "caution_gap_rate": 7.0,
        "max_3d_return": 25.0,
        "max_5d_return": 35.0,
        "max_intraday_drop_from_high": 4.0,
        "max_ma20_extension_rate": 15.0,
        "max_same_theme_in_top15": 4,
    }
    if overrides:
        base_qcfg.update(overrides)
    cfg._raw["candidate_quality_filters"] = base_qcfg
    qf = CandidateQualityFilter(cfg=cfg)
    return qf


# ---------------------------------------------------------------------------
# 빠른 제외 필터 테스트
# ---------------------------------------------------------------------------

class TestFastExclude:

    def test_etf_flag_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="069500", name="KODEX200")
        sd = _make_stock_data(symbol="069500", name="KODEX200", is_etf=True)
        passed, excluded = qf.filter_and_score([c], {c.symbol: sd})
        assert len(passed) == 0
        assert len(excluded) == 1
        assert "ETF" in excluded[0]["excluded_reason"]

    def test_etf_keyword_in_name_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="069500", name="TIGER 반도체")
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert any("TIGER" in e["excluded_reason"] for e in excluded)

    def test_etn_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="580009", name="삼성 레버리지 ETN")
        sd = _make_stock_data(symbol="580009", name="삼성 레버리지 ETN", is_etn=True)
        passed, excluded = qf.filter_and_score([c], {c.symbol: sd})
        assert len(passed) == 0

    def test_preferred_stock_excluded_by_flag(self):
        qf = _default_filter()
        c = _make_candidate(symbol="005935", name="삼성전자우")
        sd = _make_stock_data(symbol="005935", name="삼성전자우", is_preferred=True)
        passed, excluded = qf.filter_and_score([c], {c.symbol: sd})
        assert len(passed) == 0
        assert "우선주" in excluded[0]["excluded_reason"]

    def test_preferred_stock_excluded_by_name_pattern(self):
        qf = _default_filter()
        c = _make_candidate(symbol="005935", name="삼성전자우")
        # StockData 없이 이름 패턴으로 판별
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0

    def test_preferred_false_positive_avoided(self):
        """'우리금융지주' 는 우선주가 아니어야 한다."""
        qf = _default_filter()
        c = _make_candidate(symbol="316140", name="우리금융지주")
        passed, excluded = qf.filter_and_score([c])
        # 우리금융지주는 중간에 '우'가 있지만 끝이 '우'로 끝나지 않으므로 제외 안 됨
        assert len(passed) == 1

    def test_spac_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="123456", name="한화스팩28호")
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert "스팩" in excluded[0]["excluded_reason"]

    def test_reit_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="432800", name="미래에셋글로벌리츠")
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert "리츠" in excluded[0]["excluded_reason"]

    def test_halt_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="099999", name="거래정지종목")
        sd = _make_stock_data(symbol="099999", name="거래정지종목", is_halt=True)
        passed, excluded = qf.filter_and_score([c], {c.symbol: sd})
        assert len(passed) == 0

    def test_low_price_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="012345", name="동전주", current_price=500.0)
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert "동전주" in excluded[0]["excluded_reason"]

    def test_low_trading_value_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="012345", name="소형주", trade_value=1_000_000_000)
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert "거래대금" in excluded[0]["excluded_reason"]

    def test_gap_over_12_excluded(self):
        qf = _default_filter()
        c = _make_candidate(symbol="012345", name="급등주", gap_rate=13.5)
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 0
        assert "갭과다" in excluded[0]["excluded_reason"]

    def test_valid_stock_passes(self):
        qf = _default_filter()
        c = _make_candidate()
        passed, excluded = qf.filter_and_score([c])
        assert len(passed) == 1
        assert len(excluded) == 0


# ---------------------------------------------------------------------------
# 갭 모멘텀 bonus 테스트
# ---------------------------------------------------------------------------

class TestMomentumBonus:

    def test_healthy_gap_gets_bonus(self):
        """갭 2~7%: momentum_bonus > 0"""
        qf = _default_filter()
        c = _make_candidate(gap_rate=5.0)
        passed, _ = qf.filter_and_score([c])
        assert passed[0].momentum_bonus > 0

    def test_peak_gap_5pct(self):
        """갭 5% 근처에서 최대 bonus"""
        qf = _default_filter()
        c5 = _make_candidate(gap_rate=5.0)
        c3 = _make_candidate(gap_rate=3.0, symbol="000002", name="저갭")
        passed, _ = qf.filter_and_score([c5, c3])
        bonuses = {c.symbol: c.momentum_bonus for c in passed}
        assert bonuses["005930"] >= bonuses["000002"]

    def test_caution_gap_penalty(self):
        """갭 7~12%: momentum_bonus 음수"""
        qf = _default_filter()
        c = _make_candidate(gap_rate=9.0)
        passed, _ = qf.filter_and_score([c])
        assert passed[0].momentum_bonus < 0


# ---------------------------------------------------------------------------
# MA 우상향 bonus 테스트
# ---------------------------------------------------------------------------

class TestMABonus:

    def _rising_daily(self, n=25, base=60000.0, step=200.0) -> list[dict]:
        """우상향 일봉: 최신이 가장 높음 (최신 순)."""
        result = []
        for i in range(n):
            price = base + (n - 1 - i) * step
            result.append({
                "date": f"2026{i:04d}",
                "close": price,
                "open": price * 0.99,
                "high": price * 1.01,
                "low": price * 0.98,
                "volume": 1_000_000,
            })
        return result

    def test_all_ma_rising_gets_bonus(self):
        qf = _default_filter()
        c = _make_candidate(current_price=65000.0)
        daily = self._rising_daily(25, base=60000.0, step=200.0)
        # current_price (65000) > 마지막 close (60000 + 24*200 = 64800): 정배열
        passed, _ = qf.filter_and_score([c], daily_prices_cache={c.symbol: daily})
        assert passed[0].ma_bonus > 0

    def test_no_daily_data_no_ma_bonus(self):
        qf = _default_filter()
        c = _make_candidate()
        passed, _ = qf.filter_and_score([c], daily_prices_cache={})
        assert passed[0].ma_bonus == 0.0
        # warning_reason에 일봉 데이터 없음 기록
        assert "일봉" in passed[0].warning_reason or "MA" in passed[0].warning_reason


# ---------------------------------------------------------------------------
# 과열/급락 감점 테스트
# ---------------------------------------------------------------------------

class TestOverheatPenalty:

    def test_3d_surge_penalty(self):
        """3일 +30% 급등 → overheat_penalty > 0"""
        qf = _default_filter()
        c = _make_candidate(current_price=13000.0)
        # 3일 전 가격 10000 → +30%
        daily = [
            {"date": "20260617", "close": 13000, "open": 12800, "high": 13200, "low": 12700, "volume": 1000000},
            {"date": "20260616", "close": 12000, "open": 11900, "high": 12100, "low": 11800, "volume": 1000000},
            {"date": "20260615", "close": 10000, "open": 9900,  "high": 10100, "low": 9800,  "volume": 1000000},
        ] + [{"date": f"202606{i:02d}", "close": 9500, "open": 9400, "high": 9600, "low": 9300, "volume": 1000000} for i in range(1, 13)]
        passed, _ = qf.filter_and_score([c], daily_prices_cache={c.symbol: daily})
        assert passed[0].overheat_penalty > 0

    def test_5d_crash_risk_penalty(self):
        """5일 -20% 급락 → risk_penalty_q > 0"""
        qf = _default_filter()
        c = _make_candidate(current_price=8000.0)
        # 5일 전 10000 → -20%
        daily = [
            {"date": "20260617", "close": 8000,  "open": 7900, "high": 8100, "low": 7800, "volume": 1000000},
            {"date": "20260616", "close": 8500,  "open": 8400, "high": 8600, "low": 8300, "volume": 1000000},
            {"date": "20260615", "close": 9000,  "open": 8900, "high": 9100, "low": 8800, "volume": 1000000},
            {"date": "20260614", "close": 9500,  "open": 9400, "high": 9600, "low": 9300, "volume": 1000000},
            {"date": "20260613", "close": 10000, "open": 9900, "high": 10100, "low": 9800, "volume": 1000000},
        ] + [{"date": f"202606{i:02d}", "close": 10200, "open": 10100, "high": 10300, "low": 10000, "volume": 1000000} for i in range(1, 13)]
        passed, _ = qf.filter_and_score([c], daily_prices_cache={c.symbol: daily})
        assert passed[0].risk_penalty_q > 0


# ---------------------------------------------------------------------------
# 테마 cap 테스트
# ---------------------------------------------------------------------------

class TestThemeCap:

    def test_same_theme_capped_at_4(self):
        """동일 테마 5개 → 4개만 통과"""
        qf = _default_filter({"max_same_theme_in_top15": 4})
        # 5개 반도체 종목 생성
        candidates = []
        for i, (sym, name) in enumerate([
            ("000660", "SK하이닉스"),
            ("005930", "삼성전자"),
            ("042700", "한미반도체"),
            ("099800", "HPSP"),
            ("140490", "파두"),
        ]):
            c = _make_candidate(symbol=sym, name=name, rule_score=50.0 - i)
            c.theme = "semiconductor"
            c.matched_themes = "semiconductor"
            candidates.append(c)

        passed, _ = qf.filter_and_score(candidates)
        semi_in_top15 = [c for c in passed if c.theme == "semiconductor"]
        assert len(semi_in_top15) <= 4

    def test_different_themes_not_capped(self):
        """테마가 다른 4개 종목은 모두 통과"""
        qf = _default_filter({"max_same_theme_in_top15": 4})
        candidates = [
            _make_candidate(symbol="000660", name="SK하이닉스"),    # semiconductor
            _make_candidate(symbol="329180", name="현대중공업"),    # industrials
            _make_candidate(symbol="272210", name="한화에어로스페이스"),  # defense
            _make_candidate(symbol="247540", name="에코프로비엠"),   # battery
        ]
        passed, _ = qf.filter_and_score(candidates)
        assert len(passed) == 4


# ---------------------------------------------------------------------------
# 데이터 누락 시 warning 처리 테스트
# ---------------------------------------------------------------------------

class TestWarningHandling:

    def test_no_daily_data_warning_recorded(self):
        qf = _default_filter()
        c = _make_candidate()
        # daily_prices_cache 비어있음
        passed, _ = qf.filter_and_score([c], daily_prices_cache={})
        assert passed[0].warning_reason != ""

    def test_no_stock_data_still_passes(self):
        """StockData 없어도 유효 종목은 통과 (이름/코드 기반 판별)"""
        qf = _default_filter()
        c = _make_candidate()
        passed, _ = qf.filter_and_score([c], stock_data_by_symbol={})
        assert len(passed) == 1

    def test_filter_doesnt_crash_on_empty_input(self):
        qf = _default_filter()
        passed, excluded = qf.filter_and_score([])
        assert passed == []
        assert excluded == []

    def test_vwap_missing_warning_only(self):
        """VWAP 없는 일봉 데이터 → 제외 안 되고 warning_reason에만 기록"""
        qf = _default_filter()
        c = _make_candidate()
        # vwap 키 없는 daily
        daily_no_vwap = _make_daily(25)
        passed, excluded = qf.filter_and_score([c], daily_prices_cache={c.symbol: daily_no_vwap})
        assert len(passed) == 1, "VWAP 없다고 제외되면 안 됨"
        assert "VWAP" in passed[0].warning_reason

    def test_subtheme_missing_no_crash(self):
        """테마 데이터 없어도(theme='') 테마 cap 처리 중 crash 없음"""
        qf = _default_filter({"max_same_theme_in_top15": 2})
        candidates = [_make_candidate(symbol=f"00{i}930", name=f"종목{i}") for i in range(5)]
        # theme 미설정 → 각 종목이 고유 테마 취급되어 모두 통과해야 함
        passed, excluded = qf.filter_and_score(candidates)
        assert len(passed) == 5


# ---------------------------------------------------------------------------
# final_score 우선 계산 테스트
# ---------------------------------------------------------------------------

class TestFinalScorePriority:

    def test_ml_final_score_used_as_base(self):
        """final_score > 0이면 rule_score 대신 final_score를 베이스로 사용"""
        qf = _default_filter()
        # rule_score=40, final_score=70 (ML 혼합 결과)
        c = _make_candidate(rule_score=40.0)
        c.final_score = 70.0  # ML 혼합 점수
        passed, _ = qf.filter_and_score([c])
        # 보정 후 final_score는 70 기반으로 계산되어야 하므로 40보다 높아야 함
        assert passed[0].final_score > 40.0

    def test_zero_final_score_falls_back_to_rule_score(self):
        """final_score == 0이면 rule_score를 베이스로 사용"""
        qf = _default_filter()
        c = _make_candidate(rule_score=55.0)
        c.final_score = 0.0
        passed, _ = qf.filter_and_score([c])
        # rule_score(55) 기반 계산이므로 final_score > 0 이어야 함
        assert passed[0].final_score > 0.0
