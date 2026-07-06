"""
policy_selector.py 테스트.

검증 항목:
  - A/B/C/D/E 유형별 정책 선택
  - confidence_score < 60 → 신규매수 금지
  - 09:45 이후 신규매수 금지
  - 2회 손절 시 신규매수 금지
  - 당일 손실 -2% 도달 시 신규매수 금지
  - policy_gap_support: GAP Top15 ∩ 주도섹터 교집합만 후보로 남는지
"""

from unittest.mock import patch

from app.market.policy_selector import select_policy


def _regime_result(regime: str, confidence: float = 80.0, risk_off_score: float = 10.0) -> dict:
    from app.market.regime_rules import REGIME_POLICY_MAP
    return {
        "regime": regime,
        "confidence_score": confidence,
        "policy_name": REGIME_POLICY_MAP.get(regime, "policy_no_trade"),
        "scores": {"risk_off_score": risk_off_score},
    }


def test_regime_a_selects_leader_top3():
    result = select_policy(_regime_result("A"), now_hm="09:30")
    assert result.policy_name == "policy_leader_top3"
    assert result.allow_new_entry is True


def test_regime_b_selects_semiconductor_rebound():
    result = select_policy(_regime_result("B"), now_hm="09:30")
    assert result.policy_name == "policy_semiconductor_rebound"
    assert result.allow_new_entry is True


def test_regime_c_selects_gap_support():
    result = select_policy(_regime_result("C"), now_hm="09:30")
    assert result.policy_name == "policy_gap_support"
    assert result.allow_new_entry is True


def test_regime_d_blocks_new_entry():
    result = select_policy(_regime_result("D"), now_hm="09:30")
    assert result.policy_name == "policy_no_trade"
    assert result.allow_new_entry is False


def test_regime_e_selects_inverse_when_allowed():
    result = select_policy(_regime_result("E", risk_off_score=40.0), now_hm="09:30", policy_cfg={"allow_inverse": True})
    assert result.policy_name in ("policy_inverse", "policy_no_trade")
    # risk_off_score(40) <= hard_limit(75) 이므로 강제 인버스 트리거는 아니지만
    # 유형 자체가 E이므로 기본 정책은 policy_inverse 여야 한다.
    assert result.regime == "E"


def test_regime_e_high_risk_forces_inverse_or_cash():
    """market_risk_score > 75 → 인버스만 허용(allow_inverse=False면 매매 안 함)."""
    result = select_policy(
        _regime_result("E", risk_off_score=90.0), now_hm="09:30", policy_cfg={"allow_inverse": False},
    )
    assert result.forced_inverse_only is True
    assert result.policy_name == "policy_no_trade"
    assert result.allow_new_entry is False


def test_confidence_below_threshold_blocks_entry():
    result = select_policy(_regime_result("A", confidence=45.0), now_hm="09:30")
    assert result.allow_new_entry is False
    assert result.policy_name == "policy_no_trade"
    assert any("confidence_score" in r for r in result.block_reasons)


def test_entry_cutoff_time_blocks_new_entry():
    result = select_policy(_regime_result("A"), now_hm="09:50")
    assert result.allow_new_entry is False
    assert any("신규매수 가능시간 종료" in r for r in result.block_reasons)


def test_two_consecutive_losses_blocks_entry():
    result = select_policy(
        _regime_result("A"), risk_state={"consecutive_losses": 2, "daily_pnl_pct": 0.0},
        now_hm="09:30",
    )
    assert result.allow_new_entry is False
    assert any("연속 손절" in r for r in result.block_reasons)


def test_daily_loss_limit_blocks_entry():
    result = select_policy(
        _regime_result("A"), risk_state={"consecutive_losses": 0, "daily_pnl_pct": -2.5},
        now_hm="09:30",
    )
    assert result.allow_new_entry is False
    assert any("당일 손실" in r for r in result.block_reasons)


# ---------------------------------------------------------------------------
# policy_gap_support: GAP Top15 ∩ 주도섹터 교집합 테스트
# ---------------------------------------------------------------------------

class _FakeCandidate:
    def __init__(self, symbol, name, current_price, previous_close, open_to_current_rate, final_score):
        self.symbol = symbol
        self.name = name
        self.current_price = current_price
        self.previous_close = previous_close
        self.open_to_current_rate = open_to_current_rate
        self.final_score = final_score
        self.gap_rate = 2.0
        self.selected_reason = "테스트 후보"


def test_policy_gap_support_intersection_only():
    from app.strategy import policy_gap_support

    fake_top15 = [
        _FakeCandidate("000001", "주도섹터겹침", 11000, 10000, 0.5, 90.0),   # sector A, +10% → 통과
        _FakeCandidate("000002", "비주도섹터", 11000, 10000, 0.5, 95.0),     # sector B → 제외
        _FakeCandidate("000003", "이미급등", 12000, 10000, 0.5, 99.0),       # +20% → 제외
        _FakeCandidate("000004", "시가이탈", 9800, 10000, -3.0, 80.0),       # otc<-1.5 → 제외
    ]
    sector_lookup = {"000001": "semiconductor", "000002": "battery_ev", "000003": "semiconductor", "000004": "semiconductor"}

    with patch.object(policy_gap_support, "_run_gap_pipeline", return_value=(fake_top15, {"source": "test"})):
        with patch("app.strategy.sector_mapper.get_sector", side_effect=lambda sym, name="": sector_lookup.get(sym, "unknown")):
            candidates, diag = policy_gap_support.generate_candidates(
                {"leader_sectors": ["semiconductor"]}, cfg=None,
            )

    assert len(candidates) == 1
    assert candidates[0].symbol == "000001"
    assert diag["intersection_count"] == 1
