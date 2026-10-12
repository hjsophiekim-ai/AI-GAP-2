"""UI 전략 모드 라디오 — N1 / P3 / E / CHOP 네 모드 클릭 전환 (2026-10-12).

``test_ui_strategy_mode.py`` 의 N1<->P3 클릭 테스트를 네 모드 전체로 넓힌다.
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import state_store, strategy_mode
from tests.macd2.test_ui_strategy_mode import _pick, _radio, _run, _seed_mode, _texts

N1, P3, E, CHOP = (strategy_mode.MODE_N1, strategy_mode.MODE_P3,
                   strategy_mode.MODE_E, strategy_mode.MODE_CHOP)


def test_radio_lists_all_four_modes_with_labels():
    radio = _radio(_run())
    for mode in (N1, P3, E, CHOP):
        assert any(str(o).startswith(mode) for o in radio.options), radio.options


@pytest.mark.parametrize("start, target", [
    (N1, CHOP), (P3, CHOP), (CHOP, P3), (CHOP, N1), (E, CHOP), (CHOP, E),
])
def test_clicking_switches_and_persists(start, target):
    _seed_mode(start)
    at = _run()
    _pick(at, target).run()
    back = state_store.load_state()
    assert strategy_mode.current(back) == target
    assert bool(back.chop_defense_enabled) is (target == CHOP)
    assert bool(back.e_enabled) is (target == E)
    assert bool(back.p3_enabled) is strategy_mode.is_p3_based(target)


def test_chop_mode_shows_selected_mode_and_p3_status_lines():
    _seed_mode(CHOP)
    caps, _ = _texts(_run())
    assert "SELECTED MODE: **CHOP**" in caps
    # CHOP 은 P3 기반이라 REGIME/SHADOW 줄이 나와야 한다(BASE/OFF 오표시 회귀 방지)
    assert "REGIME:" in caps
    assert "SHADOW: **OFF**" not in caps
