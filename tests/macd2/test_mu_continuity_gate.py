"""MU 연속성 게이트 (2026-11-12).

T+2 패턴 판정창 [D − 30분, T+2) 안에 3분 초과 MU 분봉 공백이 있으면 mu_t2_status=GAP,
패턴 UNAVAILABLE -> 진입은 MU_ASSIST_UNAVAILABLE_BLOCK. 실사례: 2026-11-12 10:00 BLUE
(서머타임 해제 후 09:00~09:59 DNASMU→RBAQMU 공백 위에서 DIVERGE 판정 → WAIT 선택진입 → −212,015).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, mu_assist
from app.trading.macd2.models import Direction
from app.trading.macd2.config import KST

FLAG = datetime(2026, 11, 12, 10, 0, tzinfo=KST)        # D = 10:03, T+2 = 10:05, 창 = 09:33 ~ 10:04


def _series(start=datetime(2026, 11, 12, 8, 0), end=datetime(2026, 11, 12, 10, 5), holes=()):
    idx = pd.date_range(start, end, freq="1min", inclusive="left")
    s = pd.Series([800.0 - 0.05 * i for i in range(len(idx))], index=idx)
    for a, b in holes:
        s = s[(s.index < a) | (s.index >= b)]
    return s


def test_gap_inside_window_makes_pattern_unavailable():
    s = _series(holes=[(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 10, 0))])
    out = mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)
    assert out["mu_t2_status"] == mu_assist.STATUS_GAP
    assert out["mu_pattern_state"] == mu_assist.PATTERN_UNAVAILABLE
    assert mu_assist.entry_assist_status(out["mu_pattern_state"], mu_assist.HY_EXPAND) == mu_assist.ENTRY_MU_UNAVAILABLE


def test_gap_before_window_is_ignored():
    # 09:00~09:30 공백은 창(09:33~) 밖 -> 정상 판정
    s = _series(holes=[(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 9, 30))])
    out = mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)
    assert out["mu_t2_status"] == mu_assist.STATUS_OK
    assert out["mu_pattern_state"] != mu_assist.PATTERN_UNAVAILABLE


def test_single_missing_minute_at_window_edge_blocks():
    s = _series(holes=[(datetime(2026, 11, 12, 9, 33), datetime(2026, 11, 12, 9, 34))])
    assert mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)["mu_t2_status"] == mu_assist.STATUS_GAP
    s2 = _series(holes=[(datetime(2026, 11, 12, 9, 32), datetime(2026, 11, 12, 9, 33))])
    assert mu_assist.evaluate_pattern(s2, Direction.DOWN_BLUE, FLAG)["mu_t2_status"] == mu_assist.STATUS_OK


def test_toggle_off_restores_previous_behaviour(monkeypatch):
    monkeypatch.setattr(config, "MU_ASSIST_CONTINUITY_GATE_ENABLED", False)
    s = _series(holes=[(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 10, 0))])
    out = mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)
    assert out["mu_t2_status"] == mu_assist.STATUS_OK
    assert out["mu_pattern_state"] != mu_assist.PATTERN_UNAVAILABLE


def test_stale_still_takes_precedence_for_retry():
    s = _series(end=datetime(2026, 11, 12, 10, 2), holes=[(datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 10, 0))])
    assert mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)["mu_t2_status"] == mu_assist.STATUS_STALE


def _write_bars(path, minutes):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["minute_kst,feed,open,high,low,close,volume,ticks,first_kst,last_kst,first_recv,last_recv,complete"]
    for i, m in enumerate(minutes):
        feed = "DNASMU" if m.hour < 10 else "RBAQMU"
        rows.append(f"{m.isoformat()},{feed},1,1,1,{800 - 0.05 * i},1,1,,,,,1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_short_receive_gap_filled_by_loader_passes_but_longer_one_blocks(tmp_path):
    day = datetime(2026, 11, 12)
    allm = list(pd.date_range(day.replace(hour=8), day.replace(hour=10, minute=5), freq="1min", inclusive="left"))
    f = tmp_path / "mu" / "2026-11-12" / "bars_1m.csv"
    three = [m for m in allm if not (datetime(2026, 11, 12, 9, 50) <= m < datetime(2026, 11, 12, 9, 53))]
    _write_bars(f, three)
    s = mu_assist.load_mu_1m(datetime(2026, 11, 12, 10, 5, tzinfo=KST), data_dir=str(tmp_path))
    assert mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)["mu_t2_status"] == mu_assist.STATUS_OK
    four = [m for m in allm if not (datetime(2026, 11, 12, 9, 50) <= m < datetime(2026, 11, 12, 9, 54))]
    _write_bars(f, four)
    s = mu_assist.load_mu_1m(datetime(2026, 11, 12, 10, 5, tzinfo=KST), data_dir=str(tmp_path))
    assert mu_assist.evaluate_pattern(s, Direction.DOWN_BLUE, FLAG)["mu_t2_status"] == mu_assist.STATUS_GAP
