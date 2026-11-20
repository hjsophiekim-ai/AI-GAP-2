"""2026-11-19 무플래그 실사고 재발 방지 (hotfix/macd2-1m-robust-ingest).

11/19(수능일) KIS 당일 분봉 라벨이 하루 종일 HH:MM:30 이었다. 기존 코드는 3분봉 구성분
(HH:MM:00 3개)을 못 찾아 당일 봉을 전부 탈락시켰고, MACD 가 전일 19:57 봉에 멈춘 채
경보 없이 하루를 무거래로 보냈다. 여기서는
  1층 정규화(분 내림 + 원래 라벨 보존 + 분당 마지막값) / 완성 판정(원래 라벨+60초, 그 뒤 요청한 값)
  2층 입력 계약(BAR_ALIGNMENT_ANOMALY -> 신규진입만 차단, 청산·플래그 기록은 유지)
  3층 데이터 진행 감시 (a)~(d)
  4·5층 장 전 자가진단 / 장 마감 대사
를 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import (
    bar_archive, config, data_contract, data_watch, ledger, order_executor, state_store,
)
from app.trading.macd2.market_data import MarketDataService, _merge_minute_frames, filter_complete_3m_bars
from app.trading.macd2.models import Direction, PositionSnapshot
from app.trading.macd2.signal_engine import exclude_preopen_padding_1m, resample_completed_3m
from app.trading.macd2.worker import run_once
from app.utils.minute_bars import (
    RAW_TS_COL, RECEIVED_AT_COL, candles_to_minute_frame, normalize_minute_frame,
    page_cursor_before, parse_kis_minute_stamp,
)
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _rows(start: datetime, n: int, *, sec: int = 0, close: float = 100.0) -> pd.DataFrame:
    t = [start + timedelta(minutes=i, seconds=sec) for i in range(n)]
    return pd.DataFrame({"datetime": t, "open": close, "high": close, "low": close, "close": close,
                         "volume": 10})


def _candles(day: str, times: list[str], close: float = 100.0) -> list[dict]:
    return [{"date": day, "time": t, "open": close, "high": close, "low": close, "close": close,
             "volume": 1} for t in times]


# ── 1층: 정규화 ───────────────────────────────────────────────────────────

def test_a_all_30_second_labels_are_floored_and_raw_preserved():
    df, rep = normalize_minute_frame(_rows(datetime(2026, 11, 19, 10, 0, tzinfo=KST), 6, sec=30), source="t")
    assert list(df["datetime"].dt.second) == [0] * 6
    assert list(df[RAW_TS_COL].dt.second) == [30] * 6
    assert rep.nonzero_second_rows == 6 and rep.rows_out == 6


def test_b_mixed_00_and_30_in_same_minute_keeps_one_row_the_later_label():
    out, rep = candles_to_minute_frame(
        _candles("20261119", ["100500", "100530", "100600"]), source="t")
    assert len(out) == 2
    row = out.iloc[0]
    assert row["datetime"] == pd.Timestamp("2026-11-19 10:05", tz=KST)
    assert pd.Timestamp(row[RAW_TS_COL]).second == 30          # 같은 분 둘 -> 원래 라벨이 늦은 쪽
    assert rep.collapsed_duplicate_rows == 1


def test_c_intraday_format_switch_stays_contiguous_without_duplicates():
    a = _rows(datetime(2026, 11, 19, 10, 0, tzinfo=KST), 10, sec=0)
    b = _rows(datetime(2026, 11, 19, 10, 10, tzinfo=KST), 10, sec=30)
    df, _ = normalize_minute_frame(pd.concat([a, b], ignore_index=True), source="t")
    assert len(df) == 20 and df["datetime"].is_unique
    assert (df["datetime"].diff().dropna() == pd.Timedelta(minutes=1)).all()


def test_d_disk_cache_30_rows_then_new_00_rows_do_not_coexist():
    cached = _rows(datetime(2026, 11, 19, 10, 0, tzinfo=KST), 5, sec=30, close=100.0)
    cached_norm, _ = normalize_minute_frame(cached, source="cache")
    fresh = _rows(datetime(2026, 11, 19, 10, 3, tzinfo=KST), 4, sec=0, close=200.0)
    fresh_norm, _ = normalize_minute_frame(fresh, source="live")
    merged = _merge_minute_frames([cached_norm, fresh_norm])
    assert merged["datetime"].is_unique and len(merged) == 7
    last = merged.set_index("datetime")
    assert last.loc[pd.Timestamp("2026-11-19 10:03", tz=KST), "close"] == 200.0     # 최근 값
    assert pd.Timestamp(last.loc[pd.Timestamp("2026-11-19 10:03", tz=KST), RAW_TS_COL]).second == 0
    # 재정규화해도 원래 라벨을 잃지 않는다(멱등)
    again, _ = normalize_minute_frame(merged, source="again")
    assert list(again[RAW_TS_COL]) == list(merged[RAW_TS_COL])


def test_four_digit_and_odd_length_times_are_kept_with_warning():
    assert parse_kis_minute_stamp("20261119", "1005") == (datetime(2026, 11, 19, 10, 5, tzinfo=KST), "HHMM_4DIGIT")
    assert parse_kis_minute_stamp("20261119", "90500")[1] == "HMMSS_5DIGIT"
    assert parse_kis_minute_stamp("20261119", "10:05:30")[0].second == 30
    assert parse_kis_minute_stamp("20261119", "")[0] is None
    out, rep = candles_to_minute_frame(_candles("20261119", ["1005", "100600", "", "100700"]), source="t")
    assert len(out) == 3                       # 해석 불가 1행만 버림
    assert rep.unparseable_rows == 1 and rep.format_issues.get("HHMM_4DIGIT") == 1


def test_tz_naive_rows_are_localized_to_kst_and_counted():
    naive = _rows(datetime(2026, 11, 19, 10, 0), 3)
    df, rep = normalize_minute_frame(naive, source="t")
    assert str(df["datetime"].dt.tz) in ("UTC+09:00", str(KST))
    assert df["datetime"].iloc[0] == pd.Timestamp("2026-11-19 10:00", tz=KST)
    assert rep.naive_tz_rows == 3


def test_page_cursor_uses_original_label_so_30_second_pages_have_no_holes():
    """KIS 커서는 '그 시각 이전' 행을 준다. 내림 시각으로 커서를 만들면 (oldest-1분):30 행을
    건너뛰어 페이지 경계마다 구멍이 났을 것이다."""
    day = datetime(2026, 11, 19, 10, 0, tzinfo=KST)
    full = [day + timedelta(minutes=i, seconds=30) for i in range(90)]

    def fetch(mode, symbol, count, hour1):
        rows = full if not hour1 else [t for t in full if t.strftime("%H%M%S") <= hour1]
        rows = rows[-30:]
        return candles_to_minute_frame(_candles("20261119", [t.strftime("%H%M%S") for t in rows]),
                                       source="fake")[0], {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fetch,
                            fetch_minute_candles_for_date=lambda *a: (pd.DataFrame(), {}))
    svc.bootstrap(now=day + timedelta(minutes=95))
    df = svc.get_history_df()
    assert len(df) == 90 and (df["datetime"].diff().dropna() == pd.Timedelta(minutes=1)).all()
    assert page_cursor_before(df) == "095930"
    # 정상일(:00)은 기존과 같은 커서 문자열
    norm00, _ = normalize_minute_frame(_rows(day, 3), source="t")
    assert page_cursor_before(norm00) == "095900"


def test_missing_minute_mid_page_still_drops_only_that_3m_bar():
    day = datetime(2026, 11, 19, 10, 0, tzinfo=KST)
    df, _ = normalize_minute_frame(_rows(day, 30, sec=30), source="t")
    df = df[df["datetime"] != day + timedelta(minutes=10)].reset_index(drop=True)
    bars = resample_completed_3m(df, now=day + timedelta(minutes=31))
    kept, dropped = filter_complete_3m_bars(bars, df)
    assert list(dropped) == [pd.Timestamp(day + timedelta(minutes=9))]
    assert len(kept) == len(bars) - 1


# ── 1층: 완성 판정 (원래 라벨 + 60초, 그 뒤 요청한 값) ─────────────────────

def test_completion_waits_for_original_label_plus_60s():
    day = datetime(2026, 11, 19, 10, 0, tzinfo=KST)
    df, _ = normalize_minute_frame(_rows(day, 6, sec=30), source="t")
    # 10:00 봉 = 원래 라벨 10:00:30 / 10:01:30 / 10:02:30 -> 10:03:30 에 끝난다
    assert resample_completed_3m(df, now=day + timedelta(minutes=3, seconds=10)).empty
    done = resample_completed_3m(df, now=day + timedelta(minutes=3, seconds=30))
    assert list(done["datetime"]) == [pd.Timestamp(day)]
    # 정상(:00) 라벨은 기존 판정과 정확히 같다
    df00, _ = normalize_minute_frame(_rows(day, 6), source="t")
    legacy = _rows(day, 6)
    for now in (day + timedelta(minutes=3), day + timedelta(minutes=5, seconds=59), day + timedelta(minutes=6)):
        assert list(resample_completed_3m(df00, now=now)["datetime"]) == \
            list(resample_completed_3m(legacy, now=now)["datetime"])


def test_e_in_progress_snapshot_is_never_a_completed_constituent():
    day = datetime(2026, 11, 19, 10, 0, tzinfo=KST)
    df, _ = normalize_minute_frame(_rows(day, 3), source="t",
                                   received_at=day + timedelta(minutes=2, seconds=50))
    # 10:02 봉 값은 10:02:50 요청분(진행 중 스냅샷) -> 10:03:05 에도 미완성
    assert resample_completed_3m(df, now=day + timedelta(minutes=3, seconds=5)).empty
    # 10:03:02 에 다시 요청한 값이 들어오면 완성
    fresh, _ = normalize_minute_frame(_rows(day + timedelta(minutes=2), 1), source="t",
                                      received_at=day + timedelta(minutes=3, seconds=2))
    merged = _merge_minute_frames([df, fresh])
    assert len(resample_completed_3m(merged, now=day + timedelta(minutes=3, seconds=5))) == 1


def test_bar_archive_keeps_original_labels_and_replays_normalized(tmp_path):
    day = datetime(2026, 11, 19, 10, 0, tzinfo=KST)
    df, _ = normalize_minute_frame(_rows(day, 4, sec=30), source="t",
                                   received_at=day + timedelta(minutes=4, seconds=31))
    bar_archive.record_frame(df, now=day + timedelta(minutes=4, seconds=31), worker_instance_id="w")
    raw = pd.read_csv(bar_archive.archive_path("20261119"))
    assert raw["datetime"].str.endswith(":30+09:00").all()
    replay = bar_archive.load_frame_as_of("20261119", day + timedelta(minutes=5))
    assert list(replay["datetime"].dt.second) == [0] * 4
    assert list(pd.to_datetime(replay[RAW_TS_COL]).dt.second) == [30] * 4
    assert RECEIVED_AT_COL in replay.columns


# ── 2층: 입력 계약 ────────────────────────────────────────────────────────

class _S:
    def __init__(self):
        self.data_contract = {}
        self.data_contract_block_reason = None


def _frame(day, n_00, n_30_after=0, n_00_after=0):
    parts = [_rows(day, n_00)]
    if n_30_after:
        parts.append(_rows(day + timedelta(minutes=n_00), n_30_after, sec=30))
    if n_00_after:
        parts.append(_rows(day + timedelta(minutes=n_00 + n_30_after), n_00_after))
    df, _ = normalize_minute_frame(pd.concat(parts, ignore_index=True), source="t")
    return df


def test_alignment_anomaly_triggers_on_three_consecutive_and_clears_after_ten():
    day = datetime(2026, 11, 19, 9, 0, tzinfo=KST)
    s = _S()
    df = _frame(day, 40, n_30_after=2)          # 2/42 = 4.8% < 5%, 연속 2 < 3
    now = day + timedelta(minutes=42, seconds=40)
    data_contract.evaluate(s, df_1m=df, now=now, last_completed_3m_bar_at=None,
                           actionable_3m_drops=0, history_gap_active=False)
    assert s.data_contract["status"] == "OK" and s.data_contract_block_reason is None
    df = _frame(day, 40, n_30_after=3)
    data_contract.evaluate(s, df_1m=df, now=now + timedelta(minutes=1), last_completed_3m_bar_at=None,
                           actionable_3m_drops=0, history_gap_active=False)
    assert s.data_contract["status"] == data_contract.STATUS_ALIGN
    assert s.data_contract_block_reason == data_contract.STATUS_ALIGN
    df = _frame(day, 40, n_30_after=3, n_00_after=9)
    data_contract.evaluate(s, df_1m=df, now=now + timedelta(minutes=10), last_completed_3m_bar_at=None,
                           actionable_3m_drops=0, history_gap_active=False)
    assert s.data_contract["status"] == data_contract.STATUS_ALIGN          # 9개로는 해제 안 됨
    df = _frame(day, 40, n_30_after=3, n_00_after=10)
    data_contract.evaluate(s, df_1m=df, now=now + timedelta(minutes=11), last_completed_3m_bar_at=None,
                           actionable_3m_drops=0, history_gap_active=False)
    assert s.data_contract["status"] == "OK" and s.data_contract_block_reason is None
    # 해제 후에는 '해제 이후 구간' 비율만 본다 -- 하루 누적 비율로 즉시 재발동하지 않는다
    df = _frame(day, 40, n_30_after=3, n_00_after=25)
    data_contract.evaluate(s, df_1m=df, now=now + timedelta(minutes=26), last_completed_3m_bar_at=None,
                           actionable_3m_drops=0, history_gap_active=False)
    assert s.data_contract["status"] == "OK"
    rows = pd.read_csv(data_contract.ledger_path())
    assert rows["event"].str.startswith("TRANSITION").sum() == 2


def test_contract_violation_when_1m_fresh_but_3m_stuck():
    day = datetime(2026, 11, 20, 10, 0, tzinfo=KST)
    s = _S()
    df = _frame(day, 40)
    now = day + timedelta(minutes=40, seconds=5)
    data_contract.evaluate(s, df_1m=df, now=now, last_completed_3m_bar_at=(day + timedelta(minutes=30)).isoformat(),
                           actionable_3m_drops=3, history_gap_active=True)
    assert s.data_contract["status"] == data_contract.STATUS_VIOLATION
    assert any(r.startswith("STALE_3M_WITH_FRESH_1M") for r in s.data_contract["reasons"])
    assert s.data_contract["history_gap_since"] is not None


# ── 2층: 진입 차단 (청산은 유지) ───────────────────────────────────────────

def _quotes():
    return {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}


def test_executor_backstop_blocks_flat_entry_without_any_order():
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    out = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="T1", quotes=_quotes(), position=None,
        budget=1_000_000.0, new_entry_block_reason="BAR_ALIGNMENT_ANOMALY")
    assert out.block_reason == "BAR_ALIGNMENT_ANOMALY"
    assert broker.orders == []


def test_executor_backstop_still_sells_held_opposite_position_but_never_buys():
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    first = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="T0", quotes=_quotes(), position=None,
        budget=1_000_000.0)
    held = PositionSnapshot(config.INVERSE_SYMBOL, int(first.quantity), 10_000.0)
    out = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="T2", quotes=_quotes(), position=held,
        budget=1_000_000.0, new_entry_block_reason="BAR_ALIGNMENT_ANOMALY")
    sides = [(o.side, o.symbol) for o in broker.orders]
    assert sides == [("BUY", config.INVERSE_SYMBOL), ("SELL", config.INVERSE_SYMBOL)]
    assert out.block_reason == "BAR_ALIGNMENT_ANOMALY"


def _1m_from_3m_closes(start, closes, sec=0):
    rows = []
    for i, c in enumerate(closes):
        for j in range(3):
            rows.append({"datetime": start + timedelta(minutes=3 * i + j, seconds=sec),
                         "open": c, "high": c, "low": c, "close": c, "volume": 10})
    return pd.DataFrame(rows)


def _svc(df_1m, now):
    q = {config.WATCH_SYMBOL: 140.0, **_quotes()}
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
                            fetch_quote=lambda mode, symbol: (q.get(symbol), None))
    svc.bootstrap(now=now)
    svc.refresh_quotes()
    return svc


def _state():
    st = state_store.default_state()
    st.auto_trade_on = True
    st.budget = 10_000_000.0
    st.major_filter_enabled = False
    st.strategy_name, st.strategy_version, st.signal_rule = (
        config.STRATEGY_NAME, config.STRATEGY_VERSION, config.SIGNAL_RULE)
    return st


def test_worker_11_19_shape_flag_is_computed_and_recorded_but_entry_blocked():
    """11/19 재현: 라벨 전부 :30. 수정 전에는 플래그 자체가 없었다(3분봉 전부 탈락).
    수정 후 플래그는 원래 라벨+60초에 확정·원장 기록되고, 신규진입만 BAR_ALIGNMENT_ANOMALY 로 막힌다."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df = _1m_from_3m_closes(start, [100.0] * 99 + [140.0], sec=30)
    bar_end_raw = start + timedelta(minutes=3 * 100, seconds=30)       # 14:00:30
    state = _state()
    state.last_confirmed_bar_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    svc = _svc(df, bar_end_raw + timedelta(seconds=5))

    early = run_once(broker=broker, market_data=svc, state=state, now=bar_end_raw - timedelta(seconds=25))
    assert early.actions == [] and ledger.load_signal_ledger() == []   # 14:00:05 -- 아직 미완성
    assert state.data_contract_block_reason == data_contract.STATUS_ALIGN

    run_once(broker=broker, market_data=svc, state=state, now=bar_end_raw + timedelta(seconds=5))
    assert broker.orders == []
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1 and rows[0]["signal_id"].endswith("_135700_UP_RED")
    assert rows[0]["block_reason"] == data_contract.STATUS_ALIGN


def test_worker_normal_00_labels_unchanged_entry():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=300, seconds=5)
    state = _state()
    state.last_confirmed_bar_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    run_once(broker=broker, market_data=_svc(df, now), state=state, now=now)
    # 진입 여부는 기본 필터가 정한다(기존 동작). 여기서는 입력 계약이 끼어들지 않음만 고정.
    assert state.data_contract["status"] == "OK" and state.data_contract_block_reason is None
    rows = ledger.load_signal_ledger()
    assert rows and rows[0]["signal_id"].endswith("_135700_UP_RED")
    assert all(r["block_reason"] != data_contract.STATUS_ALIGN for r in rows)


# ── 3층: 데이터 진행 감시 ─────────────────────────────────────────────────

def _watch_state(**kw):
    st = state_store.default_state()
    st.auto_trade_on = True
    for k, v in kw.items():
        setattr(st, k, v)
    return st


THU = datetime(2026, 11, 19)


def _at(h, m, s=0):
    return THU.replace(hour=h, minute=m, second=s, tzinfo=KST)


def test_watch_a_last_completed_not_today_fires_at_open():
    st = _watch_state(last_completed_3m_bar_at="2026-11-18T19:57:00+09:00", today_1m_bar_count=60)
    assert "STALE_3M" in data_watch.assess(st, _at(9, 0, 15))
    assert data_watch.assess(st, _at(8, 59, 50)) == {}           # 정규장 전에는 안 본다


def test_watch_a_premarket_last_bar_is_not_stale_at_0900_but_is_after_9min():
    st = _watch_state(last_completed_3m_bar_at="2026-11-19T08:48:00+09:00")
    assert "STALE_3M" not in data_watch.assess(st, _at(9, 5))
    assert "STALE_3M" in data_watch.assess(st, _at(9, 9))
    st.last_completed_3m_bar_at = "2026-11-19T10:00:00+09:00"
    assert "STALE_3M" not in data_watch.assess(st, _at(10, 11))
    assert "STALE_3M" in data_watch.assess(st, _at(10, 12))


def test_watch_b_contract_persist_6min_and_alignment_immediate():
    dc = {"trading_date": "20261119", "status": "DATA_CONTRACT_VIOLATION",
          "since": _at(10, 0).isoformat(), "history_gap_since": _at(10, 0).isoformat()}
    st = _watch_state(last_completed_3m_bar_at="2026-11-19T10:00:00+09:00", data_contract=dc)
    assert "DATA_CONTRACT_VIOLATION" not in data_watch.assess(st, _at(10, 5))
    got = data_watch.assess(st, _at(10, 6))
    assert "DATA_CONTRACT_VIOLATION" in got and "HISTORY_GAP_PERSIST" in got
    st.data_contract = {"trading_date": "20261119", "status": "BAR_ALIGNMENT_ANOMALY",
                        "since": _at(10, 0).isoformat()}
    assert "BAR_ALIGNMENT_ANOMALY" in data_watch.assess(st, _at(10, 0, 5))


def test_watch_c_and_d_deadlines():
    st = _watch_state(last_completed_3m_bar_at="2026-11-18T19:57:00+09:00", today_completed_3m_count=0)
    assert "NO_3M_BY_0930" not in data_watch.assess(st, _at(9, 29))
    assert "NO_3M_BY_0930" in data_watch.assess(st, _at(9, 30))
    st2 = _watch_state(last_completed_3m_bar_at="2026-11-19T10:57:00+09:00", evaluated_bar_ts_today=[])
    assert "NO_EVAL_BY_1100" in data_watch.assess(st2, _at(11, 0))
    st2.evaluated_bar_ts_today = ["2026-11-19T10:57:00+09:00"]
    assert "NO_EVAL_BY_1100" not in data_watch.assess(st2, _at(11, 0))


def test_watch_ignored_when_auto_trade_off_or_weekend():
    st = _watch_state(last_completed_3m_bar_at=None)
    st.auto_trade_on = False
    assert data_watch.assess(st, _at(10, 0)) == {}
    st.auto_trade_on = True
    assert data_watch.assess(st, datetime(2026, 11, 21, 10, 0, tzinfo=KST)) == {}


def test_tracker_alerts_immediately_repeats_and_resolves():
    calls = []
    tr = data_watch.DataWatchTracker(lambda ev, **kw: calls.append((ev, kw.get("alert"))), repeat_sec=900)
    tr.observe({"STALE_3M": {}}, "mock", _at(9, 0, 15))
    tr.observe({"STALE_3M": {}}, "mock", _at(9, 5))
    tr.observe({"STALE_3M": {}}, "mock", _at(9, 15, 15))
    tr.observe({}, "mock", _at(9, 20))
    assert calls == [("DATA_STALE_3M", True), ("DATA_STALE_3M", True), ("DATA_STALE_3M_RESOLVED", True)]


def test_11_19_replay_alert_latency_with_old_and_new_code():
    """11/19 실제 상태(구버전 코드): last_completed 가 전일 19:57 에 고정, 1분봉은 신선.
    supervisor 15초 주기 기준 첫 경보 시각을 잰다 -> 정규장 시작(09:00) 첫 tick.
    (그날 실제 개장은 10:00 이었지만 감시는 09:00 정규장 기준으로 본다.)"""
    st = _watch_state(last_completed_3m_bar_at="2026-11-18T19:57:00+09:00", today_1m_bar_count=0,
                      today_completed_3m_count=0)
    calls = []
    tr = data_watch.DataWatchTracker(lambda ev, **kw: calls.append((ev, kw["now"])))
    t = _at(8, 0)
    while t < _at(11, 1) and not calls:
        tr.observe(data_watch.assess(st, t), "mock", t)
        t += timedelta(seconds=15)
    assert calls and calls[0][0] == "DATA_STALE_3M"
    assert calls[0][1] <= _at(9, 0, 15)


# ── 4·5층: 자가진단 / 대사 ───────────────────────────────────────────────

class _FakeWorker:
    def is_alive(self):
        return True


class _FakeService:
    def __init__(self, df):
        class _MD:
            _history_kis_client = None

            def get_history_df(self_inner):
                return df

            def history_updater_alive(self_inner):
                return True
        self._market_data = _MD()
        self._worker = _FakeWorker()


def test_preopen_selfcheck_flags_missing_token_and_30_second_cache():
    prior, _ = normalize_minute_frame(_rows(datetime(2026, 11, 18, 8, 0, tzinfo=KST), 720, sec=30), source="t")
    svc = _FakeService(prior)
    st = _watch_state()
    events = []
    data_watch.run_scheduled(svc, st, _at(8, 40, 5), heartbeat_age_sec=3.0,
                             record_event=lambda ev, **kw: events.append((ev, kw.get("alert"))))
    res = data_watch._read_json(data_watch.selfcheck_path())
    assert res["trading_date"] == "20261119" and not res["ok"]
    assert set(res["failed"]) == {"token", "time_format"}
    assert res["checks"]["prior_day_warmup"]["ok"] is True
    assert events == [("PREOPEN_SELFCHECK_FAILED", True)]
    # 같은 날 두 번 돌지 않는다
    data_watch.run_scheduled(svc, st, _at(8, 41), heartbeat_age_sec=3.0,
                             record_event=lambda ev, **kw: events.append(ev))
    assert len(events) == 1
    assert any("자가진단 실패" in m for m in data_watch.banner_status(_at(8, 45), st))


def test_daily_report_alerts_on_zero_flags_and_few_3m():
    st = _watch_state(today_1m_bar_count=340, today_completed_3m_count=0, evaluated_bar_ts_today=[])
    events = []
    data_watch.run_scheduled(_FakeService(None), st, _at(15, 40, 10), heartbeat_age_sec=3.0,
                             record_event=lambda ev, **kw: events.append((ev, kw.get("alert"))))
    rep = data_watch._read_json(data_watch.report_state_path())
    assert rep["verdict"] == "CHECK" and "ZERO_FLAGS" in rep["problems"]
    assert any(p.startswith("FEW_COMPLETED_3M") for p in rep["problems"])
    assert events == [("DAILY_RECONCILE_CHECK", True)]
    assert pd.read_csv(data_watch.report_ledger_path()).shape[0] == 1
