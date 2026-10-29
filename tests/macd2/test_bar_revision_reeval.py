"""완성봉 확정값 재평가 (2026-10-29 실사고: 08:09 DOWN_BLUE 소실).

실데이터(KIS NX 1분봉 10/28 08:00 ~ 10/29 08:20)로 재현한다. 08:09 봉의 마지막 구성분 08:11 은
시가 1,506,000 -> 종가 1,500,000 으로 분 후반에 급락했다. 08:12 판정 tick 이 아직 미확정인 08:11 값
(1,506,000)으로 평가하면 교차가 없고, 몇 초 뒤 확정값으로는 DOWN_BLUE 교차다.

고정하는 계약
  · 수정 전(kill switch): 미확정 값으로 한 번 평가한 봉은 다시 보지 않아 플래그·원장 행 없음 (= 사고 재현)
  · 수정 후: 다음 tick 에 값이 바뀐 것을 보고 재평가 -> 정상 경로로 신호원장에 기록(BEFORE_SESSION_OPEN),
    재평가 원장(최초평가/재평가 시각, 변경 전후 diff), 신호원장 bar_revision 컬럼
  · 같은 값이면 재평가 안 함 / 봉당 횟수 상한 / 중복 행 0 / state 저장·복원을 넘어 유지(worker 는 tick 마다 load)
  · 장중: 새 교차는 T+3 후보로 정상 등록(주문은 T+3 경로) / 사라진 교차는 주문 전 후보만 무효화,
    주문 경로가 이미 돈 플래그는 되돌리지 않고 기록만 / 지난 봉은 원장 복원만(주문·상태 변경 없음)
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import _fresh_3slot_state

KST = config.KST
DATA = Path(__file__).with_name("data") / "hynix_nx_20261028_1029_0820.csv"
T_BAR = datetime(2026, 10, 29, 8, 9, tzinfo=KST)
T_EVAL = datetime(2026, 10, 29, 8, 12, 2, tzinfo=KST)          # 첫 판정 tick (08:11 미확정)
SID = "20261029_080900_DOWN_BLUE"
PARTIAL_0811_CLOSE = 1_506_000.0                               # 08:11 분봉의 시가 = 분 초반 값


def _frame(shift: timedelta = timedelta(0)) -> pd.DataFrame:
    df = pd.read_csv(DATA, parse_dates=["datetime"])
    df["datetime"] = (df["datetime"] + shift).dt.tz_localize(KST)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def _partial(df: pd.DataFrame, minute: datetime) -> pd.DataFrame:
    x = df.copy()
    m = x["datetime"] == pd.Timestamp(minute)
    x.loc[m, "close"] = PARTIAL_0811_CLOSE
    x.loc[m, "low"] = PARTIAL_0811_CLOSE
    return x


def _svc(df: pd.DataFrame) -> MarketDataService:
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a, **k: (df, {}),
                            fetch_quote=lambda mode, sym: ({config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0,
                                                            config.WATCH_SYMBOL: 1_500_000.0}.get(sym), None))
    svc.bootstrap(now=T_EVAL)
    svc.refresh_quotes()
    return svc


def _set_frame(svc, df, now):
    with svc._history_lock:
        svc._df_1m = df[df["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)].reset_index(drop=True)


def _tick(svc, state, broker, df, now):
    """worker 루프와 같게 tick 마다 state 를 저장했다가 다시 읽는다."""
    _set_frame(svc, df, now)
    st = state_store.load_state()
    r = run_once(broker=broker, market_data=svc, state=st, now=now)
    state_store.save_state(st)
    return st, r


def _rows(prefix=SID):
    return [r for r in ledger._load_rows(ledger.SIGNAL_LEDGER_PATH, limit=0) if r.get("signal_id", "").startswith(prefix)]


def _rev_rows():
    p = Path(ledger.SIGNAL_LEDGER_PATH).parent / config.BAR_REVISION_LEDGER_FILENAME
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []


def _start(df_first, *, shift=timedelta(0)):
    svc = _svc(df_first)
    st = _fresh_3slot_state()
    st.auto_trade_on = True
    state_store.save_state(st)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    return svc, broker


def _run_premarket(enabled: bool, monkeypatch):
    monkeypatch.setattr(config, "BAR_REVISION_RECHECK_ENABLED", enabled)
    final = _frame()
    partial = _partial(final, datetime(2026, 10, 29, 8, 11, tzinfo=KST))
    svc, broker = _start(partial)
    # 08:06 ~ 08:12 : 08:11 미확정 값 상태로 진행(08:12:02 첫 판정)
    for now in (datetime(2026, 10, 29, 8, 9, 2, tzinfo=KST), T_EVAL):
        st, r = _tick(svc, None, broker, partial, now)
    first_eval_rows = _rows()
    # 08:12:07 : KIS 확정값 도착 -> 같은 봉이 아직 최신
    st, r = _tick(svc, None, broker, final, T_EVAL + timedelta(seconds=5))
    st, r2 = _tick(svc, None, broker, final, T_EVAL + timedelta(seconds=10))
    return first_eval_rows, st, r, r2, broker


# ── 오늘 08:09 BLUE 재현 ────────────────────────────────────────────────────

def test_final_data_really_has_the_0809_blue_cross():
    from app.trading.macd2 import signal_engine as se
    df = _frame()
    b3 = se.resample_completed_3m(df[df.datetime < T_EVAL], T_EVAL)
    snap = se.calculate_macd(b3)
    assert pd.Timestamp(snap.bar_dt) == pd.Timestamp(T_BAR)
    assert snap.previous_diff > 0 > snap.current_diff
    b3p = se.resample_completed_3m(_partial(df, datetime(2026, 10, 29, 8, 11, tzinfo=KST))[lambda d: d.datetime < T_EVAL], T_EVAL)
    sp = se.calculate_macd(b3p)
    assert sp.previous_diff > 0 and sp.current_diff > 0, "미확정 08:11 값이면 교차 없음"


def test_before_fix_flag_is_lost(monkeypatch):
    first, st, r, r2, broker = _run_premarket(False, monkeypatch)
    assert first == [] and _rows() == [], "kill switch(=수정 전): 08:09 BLUE 영구 누락"
    assert _rev_rows() == []


def test_after_fix_flag_is_restored_automatically(monkeypatch):
    first, st, r, r2, broker = _run_premarket(True, monkeypatch)
    assert first == [], "첫 판정은 미확정 값이라 교차 없음(사고와 같은 출발점)"
    rows = _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["signal_id"] == SID and row["block_reason"] == "BEFORE_SESSION_OPEN"
    assert "BAR_REVISION_REPLAY" in row.get("bar_revision", "") and "first=" in row["bar_revision"]
    assert any(a.startswith("BAR_REVISION:NEW_FLAG_LIVE_PATH") for a in r.actions), r.actions
    rev = [x for x in _rev_rows() if x["bar_at"].startswith("2026-10-29T08:09")]
    assert len(rev) == 1 and rev[0]["action"] == "NEW_FLAG_LIVE_PATH" and rev[0]["direction_after"] == "DOWN_BLUE"
    assert float(rev[0]["cur_diff_before"]) > 0 > float(rev[0]["cur_diff_after"])
    assert rev[0]["first_evaluated_at"].startswith("2026-10-29T08:12:02") and rev[0]["reevaluated_at"].startswith("2026-10-29T08:12:07")
    assert st.last_detected_direction == Direction.DOWN_BLUE
    # 같은 값의 다음 tick: 재평가 없음, 중복 행 없음, 주문 없음(장전)
    assert not any(a.startswith("BAR_REVISION") for a in r2.actions)
    assert len(_rows()) == 1 and [o for o in broker.orders if o.success] == []


def test_recheck_count_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "BAR_REVISION_RECHECK_ENABLED", True)
    final = _frame()
    partial = _partial(final, datetime(2026, 10, 29, 8, 11, tzinfo=KST))
    svc, broker = _start(partial)
    _tick(svc, None, broker, partial, T_EVAL)
    for i in range(1, 9):                       # 값이 계속 뒤집히는 극단
        _tick(svc, None, broker, final if i % 2 else partial, T_EVAL + timedelta(seconds=5 * i))
    rev = [x for x in _rev_rows() if x["bar_at"].startswith("2026-10-29T08:09")]
    assert len(rev) == config.BAR_REVISION_MAX_RECHECKS
    assert len(_rows(SID)) <= 1, "같은 signal_id 중복 행 0"


# ── 장중: T+3 후보 / 무효화 / 주문 후 불변 / 지난 봉 ─────────────────────────

INTRA = timedelta(hours=2)                       # 같은 실데이터를 10:09 봉으로 옮긴 장중 시나리오
I_BAR = T_BAR + INTRA
I_EVAL = T_EVAL + INTRA
I_SID = "20261029_100900_DOWN_BLUE"


def _intraday(monkeypatch, first_df, second_df, *, mark_ordered=False, second_at=I_EVAL + timedelta(seconds=5)):
    monkeypatch.setattr(config, "BAR_REVISION_RECHECK_ENABLED", True)
    svc, broker = _start(first_df)
    _tick(svc, None, broker, first_df, I_EVAL)
    if mark_ordered:
        assert ledger.try_claim_signal_dispatch(I_SID, "BUY"), "주문 직전에만 생기는 dispatch claim 으로 '주문됨' 표시"
    st, r = _tick(svc, None, broker, second_df, second_at)
    return st, r, broker


def _intra_frames():
    final = _frame(INTRA)
    partial = _partial(final, datetime(2026, 10, 29, 10, 11, tzinfo=KST))
    return final, partial


def test_intraday_new_cross_registers_t3_candidate_without_retro_order(monkeypatch):
    final, partial = _intra_frames()
    st, r, broker = _intraday(monkeypatch, partial, final)
    assert st.tw2_3slot_pending_flag_bar_ts == I_BAR.isoformat()
    assert st.tw2_3slot_pending_flag_direction == Direction.DOWN_BLUE
    assert [o for o in broker.orders if o.success] == [], "플래그 tick 에 소급 주문 없음 — T+3 에서 판정"
    assert len(_rows(I_SID)) >= 1


def test_intraday_vanished_cross_revokes_candidate_before_order(monkeypatch):
    final, partial = _intra_frames()
    st, r, broker = _intraday(monkeypatch, final, partial)      # 처음엔 교차, 확정값에선 사라짐
    assert st.tw2_3slot_pending_flag_direction is None and st.tw2_3slot_pending_flag_bar_ts is None
    assert st.last_detected_direction != Direction.DOWN_BLUE
    assert any(x["signal_id"] == f"{I_SID}:FLAG_REVOKED_BY_REVISION" for x in _rows(I_SID))
    rev = [x for x in _rev_rows() if x["bar_at"].startswith("2026-10-29T10:09")]
    assert rev and rev[-1]["action"] == "PRIOR_FLAG_REVOKED_BEFORE_ORDER"


def test_vanished_cross_after_order_path_is_log_only(monkeypatch):
    final, partial = _intra_frames()
    st, r, broker = _intraday(monkeypatch, final, partial, mark_ordered=True)
    assert st.last_detected_direction == Direction.DOWN_BLUE, "주문 경로가 돈 플래그는 되돌리지 않는다"
    rev = [x for x in _rev_rows() if x["bar_at"].startswith("2026-10-29T10:09")]
    assert rev and rev[-1]["action"] == "PRIOR_FLAG_VANISHED_ORDER_KEPT_LOG_ONLY"


def test_revision_after_next_bar_is_ledger_only(monkeypatch):
    """확정값이 다음 봉 완성 뒤에야 오면 원장 복원만 하고 T+3 후보·방향 상태·주문은 건드리지 않는다."""
    final, partial = _intra_frames()
    late = I_EVAL + timedelta(minutes=3, seconds=5)               # 10:12 봉 완성 뒤
    st, r, broker = _intraday(monkeypatch, partial, final, second_at=late)
    assert any(x["signal_id"] == I_SID and x["block_reason"] == "BAR_REVISION_REPLAY" for x in _rows(I_SID))
    assert st.tw2_3slot_pending_flag_bar_ts != I_BAR.isoformat()
    assert [o for o in broker.orders if o.success] == []
