"""MU 예정 공백 대체확인 (2026-11-04) — scheduled_gap 판정과 원장 필드.

예정 공백 = KST [09:00, 주간거래 시작) ∧ 그 구간 MU 실분봉이 이어지지 않음 ∧ DNASMU 가 08:55 이후까지 들어옴
(월요일은 DNAS 없음 허용). 서머타임(주간거래 09:00)엔 구간이 비어 항상 False.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.trading.macd2 import config, mu_assist


def _series(day, start, end, *, extra=()):
    idx = list(pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", inclusive="left")) + \
        [pd.Timestamp(f"{day} {x}") for x in extra]
    s = pd.Series(900.0, index=pd.DatetimeIndex(sorted(idx)))
    s.attrs["last_real_minute"] = s.index.max() if len(s) else None
    return s


@pytest.mark.parametrize("now, series, expect", [
    # 11/03(화, EST): DNAS 08:00~08:59, 09:30 판정 -> 예정 공백
    (datetime(2026, 11, 3, 9, 30), _series("2026-11-03", "08:00", "09:00"), (True, mu_assist.GAP_SCHEDULED)),
    # 09:00 잔여 체결 1분봉이 있어도 공백 (11/04 raw frames)
    (datetime(2026, 11, 3, 9, 30), _series("2026-11-03", "08:00", "09:01"), (True, mu_assist.GAP_SCHEDULED)),
    # 9시대 MU 가 이어지면(REST/WS 복구) 공백 아님
    (datetime(2026, 11, 3, 9, 30), _series("2026-11-03", "08:00", "09:20"), (False, mu_assist.GAP_MU_PRESENT)),
    # DNAS 가 08:30 에 끊겼다 = 로거/피드 장애 -> 공백 아님
    (datetime(2026, 11, 3, 9, 30), _series("2026-11-03", "08:00", "08:31"), (False, mu_assist.GAP_DNAS_ENDED_EARLY)),
    # 평일 MU 데이터 자체가 없음 = 장애 -> 공백 아님
    (datetime(2026, 11, 3, 9, 30), None, (False, mu_assist.GAP_NO_MU)),
    # 월요일(미국 일요일) DNAS 없음 -> 예정 공백
    (datetime(2026, 11, 2, 9, 30), None, (True, mu_assist.GAP_SCHEDULED)),
    # 10:00 이후 / 09:00 이전 -> 구간 밖
    (datetime(2026, 11, 3, 10, 0), _series("2026-11-03", "08:00", "09:00"), (False, mu_assist.GAP_OUTSIDE_WINDOW)),
    (datetime(2026, 11, 3, 8, 59), _series("2026-11-03", "08:00", "08:59"), (False, mu_assist.GAP_OUTSIDE_WINDOW)),
    # 서머타임(10/28) -> 주간거래 09:00 시작이라 구간 없음
    (datetime(2026, 10, 28, 9, 30), _series("2026-10-28", "08:00", "09:00"), (False, mu_assist.GAP_OUTSIDE_WINDOW)),
])
def test_scheduled_gap(now, series, expect, monkeypatch):
    monkeypatch.setattr(mu_assist, "load_mu_1m", lambda *a, **k: None)
    s = series if series is not None else pd.Series(dtype=float)
    assert mu_assist.scheduled_gap(now, s) == expect


def test_future_minutes_in_file_are_ignored():
    """재생 파일엔 10:00 이후 RBAQ 분봉이 이미 있다 -> 판정 시각 이후 분은 보지 않는다."""
    s = _series("2026-11-03", "08:00", "09:00", extra=[f"10:{m:02d}" for m in range(30)])
    assert mu_assist.scheduled_gap(datetime(2026, 11, 3, 9, 30), s) == (True, mu_assist.GAP_SCHEDULED)


def test_default_off_and_ledger_fields():
    assert config.MU_GAP_FALLBACK_ENABLED is False
    assert mu_assist.gap_ledger_fields(None) == {}
    f = mu_assist.gap_ledger_fields({"reason": mu_assist.GAP_SCHEDULED, "cttr": 88.123456, "cttr_state": "MATCH",
                                     "opposite_gap": 207.23336, "decision": mu_assist.WAIT_SELECTIVE_APPROVED})
    assert f == {"mu_gap_fallback": True, "mu_gap_reason": "SCHEDULED_GAP", "mu_gap_exec_strength": 88.123,
                 "mu_gap_exec_strength_state": "MATCH", "mu_gap_opposite_gap": 207.2334,
                 "mu_gap_decision": mu_assist.WAIT_SELECTIVE_APPROVED}
    from app.trading.macd2 import ledger
    assert set(f) <= set(ledger.SIGNAL_LEDGER_COLUMNS)
