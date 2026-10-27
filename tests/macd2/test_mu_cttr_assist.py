"""MU Assist × 체결강도 (2026-10-26, feature/macd2-mu-cttr-assist) — exec_strength + worker 판정 헬퍼.

고정하는 계약
-------------
0. 세 토글 모두 기본 OFF, OFF 면 헬퍼는 기존 판정을 바꾸지 않는다(TEG 거절 유지 / veto 없음 / NO_REENTRY 유지)
1. 체결강도 = 직전 60초 cttr 평균, RED >100 / BLUE <100 이 MATCH, 정확히 100 은 MISMATCH, 데이터 없으면 UNAVAILABLE
2. MATCH_OVERRIDE: 사유가 recent_cross / 9분 뿐 ∧ MATCH ∧ MU ENTRY_CONFIRM 일 때만 완화. EMA/VWAP 사유가 있으면 불가
3. MISMATCH_VETO: MISMATCH ∧ MU STALL/DIVERGE 일 때만 차단. MU CONFIRM·기록없음·체결강도 판정불가면 통과
4. EXIT_REENTRY: MU ENTRY_CONFIRM 또는 MATCH 면 허용, 둘 다 아니면 기존 NO_REENTRY
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import config, exec_strength, mu_assist, teg_gate, worker
from app.trading.macd2.models import Direction, RuntimeState

KST = config.KST
NOW = datetime(2026, 10, 26, 12, 39, 0, tzinfo=KST)
SID = "20261026_123300_UP_RED:TW2_3SLOT_CONFIRM"


def _write_snaps(root: Path, now: datetime, values: list[float], age_sec: int = 0) -> None:
    """now 직전 len(values) 초에 1초 간격 스냅샷. age_sec 만큼 더 과거로 민다(stale 재현)."""
    d = root / "snapshots" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    base = now.replace(tzinfo=None) - timedelta(seconds=len(values) + age_sec)
    lines = ["ts,symbol,price,cttr"]
    for i, v in enumerate(values):
        lines.append(f"{(base + timedelta(seconds=i)).isoformat()},660,1000000,{v}")
    (d / "000660.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_STRENGTH_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def mu_on(monkeypatch):
    state = RuntimeState()
    state.mu_assist_enabled = True
    return state


def _mu(monkeypatch, status: str = mu_assist.ENTRY_CONFIRM, pattern: str = mu_assist.PATTERN_CONFIRM):
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: status)
    monkeypatch.setattr(mu_assist, "entry_pattern_for", lambda sid: pattern)


# ── 0. 기본값 ─────────────────────────────────────────────────────────────

def test_toggles_default_off():
    assert config.MU_CTTR_MATCH_OVERRIDE is False
    assert config.MU_CTTR_MISMATCH_VETO is False
    assert config.MU_EXIT_REENTRY is False


def test_off_keeps_existing_decisions(snapdir, mu_on, monkeypatch):
    _write_snaps(snapdir, NOW, [120.0] * 60)
    _mu(monkeypatch)
    m: dict = {}
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL,), m) is False
    assert worker._mu_exit_reentry_allowed(SID, Direction.UP_RED, NOW) is False


# ── 1. 체결강도 정의 ───────────────────────────────────────────────────────

def test_mean_and_state(snapdir):
    _write_snaps(snapdir, NOW, [90.0] * 30 + [130.0] * 30)
    assert exec_strength.mean_cttr(NOW) == pytest.approx(110.0)
    assert exec_strength.evaluate(Direction.UP_RED, NOW)["state"] == exec_strength.MATCH
    assert exec_strength.evaluate(Direction.DOWN_BLUE, NOW)["state"] == exec_strength.MISMATCH


def test_exactly_100_is_mismatch_both_ways():
    assert exec_strength.state_for(Direction.UP_RED, 100.0) == exec_strength.MISMATCH
    assert exec_strength.state_for(Direction.DOWN_BLUE, 100.0) == exec_strength.MISMATCH


def test_stale_or_missing_is_unavailable(snapdir):
    assert exec_strength.evaluate(Direction.UP_RED, NOW)["state"] == exec_strength.UNAVAILABLE   # 파일 없음
    _write_snaps(snapdir, NOW, [120.0] * 10, age_sec=120)                                          # 2분 전 데이터만
    assert exec_strength.evaluate(Direction.UP_RED, NOW)["state"] == exec_strength.UNAVAILABLE


def test_window_excludes_now_and_older_than_60s(snapdir):
    _write_snaps(snapdir, NOW, [50.0] * 30 + [150.0] * 60)        # 앞 30초는 60초 창 밖
    assert exec_strength.mean_cttr(NOW) == pytest.approx(150.0)


# ── 2. MATCH_OVERRIDE ────────────────────────────────────────────────────

@pytest.fixture
def override_on(monkeypatch):
    monkeypatch.setattr(config, "MU_CTTR_MATCH_OVERRIDE", True)


@pytest.mark.parametrize("reasons", [(teg_gate.COND_MIN_INTERVAL,), (teg_gate.COND_RECENT_CROSS,),
                                     (teg_gate.COND_RECENT_CROSS, teg_gate.COND_MIN_INTERVAL)])
def test_override_time_like_reasons(snapdir, mu_on, monkeypatch, override_on, reasons):
    _write_snaps(snapdir, NOW, [120.0] * 60)
    _mu(monkeypatch)
    m: dict = {}
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, reasons, m) is True
    assert m["mu_cttr_teg_override"] == sorted(reasons)


@pytest.mark.parametrize("extra", [teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP, teg_gate.COND_MACD_GAP_EXPANDING])
def test_override_never_with_ema_vwap_gap(snapdir, mu_on, monkeypatch, override_on, extra):
    """10/26 12:39 RED 형태(EMA+VWAP+9분)는 완화되지 않는다."""
    _write_snaps(snapdir, NOW, [120.0] * 60)
    _mu(monkeypatch)
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL, extra), {}) is False


@pytest.mark.parametrize("status", [mu_assist.ENTRY_WAIT, mu_assist.ENTRY_BAD, mu_assist.ENTRY_MU_UNAVAILABLE, ""])
def test_override_requires_mu_entry_confirm(snapdir, mu_on, monkeypatch, override_on, status):
    _write_snaps(snapdir, NOW, [120.0] * 60)
    _mu(monkeypatch, status=status)
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL,), {}) is False


def test_override_requires_match_and_available(snapdir, mu_on, monkeypatch, override_on):
    _mu(monkeypatch)
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL,), {}) is False
    _write_snaps(snapdir, NOW, [80.0] * 60)
    assert worker._mu_cttr_teg_override(mu_on, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL,), {}) is False


def test_override_requires_mu_assist_on(snapdir, monkeypatch, override_on):
    _write_snaps(snapdir, NOW, [120.0] * 60)
    _mu(monkeypatch)
    state = RuntimeState()
    state.mu_assist_enabled = False
    assert worker._mu_cttr_teg_override(state, SID, Direction.UP_RED, NOW, (teg_gate.COND_MIN_INTERVAL,), {}) is False


# ── 3. MISMATCH_VETO ─────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern", [mu_assist.PATTERN_STALL, mu_assist.PATTERN_DIVERGE])
def test_veto_mismatch_and_weak_mu(snapdir, monkeypatch, pattern):
    _write_snaps(snapdir, NOW, [110.0] * 60)          # BLUE 에 불일치
    _mu(monkeypatch, status=mu_assist.ENTRY_WAIT, pattern=pattern)
    assert worker._mu_cttr_mismatch_veto(SID, Direction.DOWN_BLUE, NOW) == config.MU_CTTR_MISMATCH_VETO_BLOCK_REASON


@pytest.mark.parametrize("pattern", [mu_assist.PATTERN_CONFIRM, mu_assist.PATTERN_UNAVAILABLE, ""])
def test_no_veto_when_mu_confirm_or_unknown(snapdir, monkeypatch, pattern):
    """10/26 10:45·14:33 BLUE 형태(불일치지만 MU CONFIRM)는 막지 않는다."""
    _write_snaps(snapdir, NOW, [110.0] * 60)
    _mu(monkeypatch, pattern=pattern)
    assert worker._mu_cttr_mismatch_veto(SID, Direction.DOWN_BLUE, NOW) is None


def test_no_veto_when_match_or_cttr_unavailable(snapdir, monkeypatch):
    _mu(monkeypatch, status=mu_assist.ENTRY_WAIT, pattern=mu_assist.PATTERN_STALL)
    assert worker._mu_cttr_mismatch_veto(SID, Direction.DOWN_BLUE, NOW) is None     # 파일 없음
    _write_snaps(snapdir, NOW, [90.0] * 60)
    assert worker._mu_cttr_mismatch_veto(SID, Direction.DOWN_BLUE, NOW) is None     # 일치


# ── 4. EXIT_REENTRY ──────────────────────────────────────────────────────

@pytest.fixture
def reentry_on(monkeypatch):
    monkeypatch.setattr(config, "MU_EXIT_REENTRY", True)


def test_reentry_on_mu_confirm_even_if_mismatch(snapdir, monkeypatch, reentry_on):
    """10/26 14:33 BLUE: 체결강도 불일치(110.9) + MU ENTRY_CONFIRM → 허용."""
    _write_snaps(snapdir, NOW, [110.9] * 60)
    _mu(monkeypatch)
    assert worker._mu_exit_reentry_allowed(SID, Direction.DOWN_BLUE, NOW) is True


def test_reentry_on_match_without_mu_confirm(snapdir, monkeypatch, reentry_on):
    _write_snaps(snapdir, NOW, [90.0] * 60)
    _mu(monkeypatch, status=mu_assist.ENTRY_WAIT)
    assert worker._mu_exit_reentry_allowed(SID, Direction.DOWN_BLUE, NOW) is True


def test_reentry_blocked_when_neither(snapdir, monkeypatch, reentry_on):
    _write_snaps(snapdir, NOW, [110.0] * 60)
    _mu(monkeypatch, status=mu_assist.ENTRY_WAIT)
    assert worker._mu_exit_reentry_allowed(SID, Direction.DOWN_BLUE, NOW) is False
    _mu(monkeypatch, status="")
    assert worker._mu_exit_reentry_allowed(SID, Direction.DOWN_BLUE, NOW) is False   # 판정 불가 → 기존 금지
