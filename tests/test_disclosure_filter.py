"""
Tests for DisclosureFilter keyword classification.
No external API calls required.
"""
import pytest
from app.data.disclosure_filter import DisclosureFilter


class _FakeCfg:
    dart = {
        "enabled": True,
        "max_positive_bonus": 10,
        "max_negative_penalty": -20,
        "exclude_severe_risk_disclosure": True,
    }
    _raw = {}


@pytest.fixture
def disc_filter():
    return DisclosureFilter(cfg=_FakeCfg())


def _disc(title: str) -> dict:
    return {"report_nm": title}


# ── 긍정 키워드 ────────────────────────────────────────────────────────────

def test_positive_strong_contract(disc_filter):
    result = disc_filter.score_disclosures([_disc("단일판매공급계약체결")])
    assert result["disclosure_score"] > 0


def test_positive_buyback(disc_filter):
    result = disc_filter.score_disclosures([_disc("자기주식취득 결정")])
    assert result["disclosure_score"] > 0
    assert result["positive_count"] >= 1


def test_positive_bonus_shares(disc_filter):
    result = disc_filter.score_disclosures([_disc("무상증자 결정")])
    assert result["disclosure_score"] > 0


def test_positive_tech_transfer(disc_filter):
    result = disc_filter.score_disclosures([_disc("기술이전 계약 체결")])
    assert result["disclosure_score"] > 0


# ── 부정 키워드 ────────────────────────────────────────────────────────────

def test_negative_rights_offering(disc_filter):
    result = disc_filter.score_disclosures([_disc("유상증자 결정")])
    assert result["disclosure_score"] < 0


def test_negative_lawsuit(disc_filter):
    result = disc_filter.score_disclosures([_disc("소송 제기에 관한 공시")])
    assert result["disclosure_score"] < 0


def test_negative_convertible_bond(disc_filter):
    result = disc_filter.score_disclosures([_disc("전환사채 발행 결정")])
    assert result["disclosure_score"] < 0


# ── 강한 리스크 키워드 ─────────────────────────────────────────────────────

def test_severe_risk_delisting(disc_filter):
    result = disc_filter.score_disclosures([_disc("상장폐지 예고")])
    assert result["has_severe_risk"] is True
    assert result["disclosure_score"] <= -20


def test_severe_risk_embezzlement(disc_filter):
    result = disc_filter.score_disclosures([_disc("횡령배임 혐의 고발")])
    assert result["has_severe_risk"] is True


def test_severe_risk_audit_denial(disc_filter):
    result = disc_filter.score_disclosures([_disc("감사의견 거절")])
    assert result["has_severe_risk"] is True


def test_severe_risk_management_issue(disc_filter):
    result = disc_filter.score_disclosures([_disc("관리종목 지정")])
    assert result["has_severe_risk"] is True


# ── 점수 범위 제한 ─────────────────────────────────────────────────────────

def test_score_capped_positive(disc_filter):
    """여러 긍정 공시가 있어도 최대 +10점으로 제한."""
    disclosures = [
        _disc("단일판매공급계약체결"),
        _disc("자기주식취득 결정"),
        _disc("무상증자 결정"),
        _disc("기술이전 계약"),
        _disc("품목허가 취득"),
    ]
    result = disc_filter.score_disclosures(disclosures)
    assert result["disclosure_score"] <= 10


def test_score_capped_negative(disc_filter):
    """여러 리스크 공시가 있어도 최소 -20점으로 제한."""
    disclosures = [
        _disc("상장폐지 예고"),
        _disc("횡령배임 혐의"),
        _disc("영업정지 처분"),
        _disc("관리종목 지정"),
    ]
    result = disc_filter.score_disclosures(disclosures)
    assert result["disclosure_score"] >= -20


# ── 빈 공시 ────────────────────────────────────────────────────────────────

def test_empty_disclosures(disc_filter):
    result = disc_filter.score_disclosures([])
    assert result["disclosure_score"] == 0.0
    assert result["has_severe_risk"] is False


# ── should_exclude ──────────────────────────────────────────────────────────

def test_should_exclude_severe(disc_filter):
    result = disc_filter.score_disclosures([_disc("상장폐지 예고")])
    assert disc_filter.should_exclude(result) is True


def test_should_not_exclude_normal(disc_filter):
    result = disc_filter.score_disclosures([_disc("자기주식취득 결정")])
    assert disc_filter.should_exclude(result) is False


# ── score_all ──────────────────────────────────────────────────────────────

def test_score_all(disc_filter):
    data = {
        "005930": [_disc("자기주식취득 결정")],
        "000660": [_disc("상장폐지 예고")],
        "035420": [],
    }
    results = disc_filter.score_all(data)
    assert results["005930"]["disclosure_score"] > 0
    assert results["000660"]["has_severe_risk"] is True
    assert results["035420"]["disclosure_score"] == 0.0
