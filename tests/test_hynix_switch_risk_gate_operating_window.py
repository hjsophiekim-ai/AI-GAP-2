"""test_hynix_switch_risk_gate_operating_window.py — 2026-08-23 회귀 테스트.

is_within_operating_window()이 시각(time-of-day)만 보고 요일을 확인하지 않아
주말에도 평일 장중처럼 True를 반환하던 버그의 재발 방지. 평일 08:50~15:30 기존
동작은 절대 바뀌면 안 된다."""

from __future__ import annotations

from datetime import datetime

from app.trading.hynix_switch_risk_gate import is_within_operating_window

# 2026-08-24(월)~28(금) 평일, 22(토)/23(일) 주말 — 실제 달력 기준.
_WEEKDAY_NOON = datetime(2026, 8, 24, 13, 0)  # 월요일 13:00
_SATURDAY_NOON = datetime(2026, 8, 22, 13, 0)  # 토요일 13:00
_SUNDAY_NOON = datetime(2026, 8, 23, 13, 0)  # 일요일 13:00


def test_saturday_is_always_outside_operating_window():
    assert is_within_operating_window(_SATURDAY_NOON) is False


def test_sunday_is_always_outside_operating_window():
    assert is_within_operating_window(_SUNDAY_NOON) is False


def test_saturday_before_market_hours_is_still_outside_window():
    # 요일 체크가 시각 체크보다 먼저 적용되는지 확인 — 새벽 시간이라 원래도
    # False였을 케이스와 섞이지 않도록 정오로 고정했지만, 자정 근처도 함께 확인.
    assert is_within_operating_window(datetime(2026, 8, 22, 0, 30)) is False


def test_weekday_regular_hours_still_true_after_fix():
    """평일 08:50~15:30 동작은 이번 수정으로 절대 바뀌지 않아야 한다."""
    assert is_within_operating_window(_WEEKDAY_NOON) is True
    assert is_within_operating_window(datetime(2026, 8, 24, 8, 50)) is True  # 시작 경계(포함)
    assert is_within_operating_window(datetime(2026, 8, 24, 15, 29, 59)) is True  # 종료 직전


def test_weekday_outside_window_hours_still_false_after_fix():
    assert is_within_operating_window(datetime(2026, 8, 24, 8, 49, 59)) is False  # 시작 직전
    assert is_within_operating_window(datetime(2026, 8, 24, 15, 30)) is False  # 종료 경계(제외)
    assert is_within_operating_window(datetime(2026, 8, 24, 20, 0)) is False  # 야간
