"""MU REST fallback 분봉 병합 (2026-11-04) — load_mu_1m 이 rest_bars_1m.csv 를 쓰는 규칙.

  · [09:00, 주간거래 시작) 에서 웹소켓 분봉이 **없는** 분에만 REST 값을 쓴다(웹소켓 우선).
  · 서머타임(주간거래 09:00 시작) 날은 구간이 비어 REST 파일이 있어도 결과가 기존과 같다.
  · 파일이 없거나 MACD2_MU_REST_FALLBACK_ENABLED=False 면 기존과 완전히 같다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, mu_assist
from tests.macd2.test_mu_feed_dst import _load, _write


def _rest(root, day, start, end, *, px0=500.0, feed=config.MU_REST_FEED, complete=1, extra=()):
    d = root / "mu" / day
    d.mkdir(parents=True, exist_ok=True)
    rows = ["minute_kst,feed,open,high,low,close,volume,ticks,first_kst,last_kst,first_recv,last_recv,complete"]
    t = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M")
    e = datetime.strptime(f"{day} {end}", "%Y-%m-%d %H:%M")
    px = px0
    while t < e:
        px += 0.5
        rows.append(f"{t.isoformat()},{feed},{px},{px},{px},{px},10,0,,,,,{complete}")
        t += timedelta(minutes=1)
    rows.extend(extra)
    (d / config.MU_REST_FILENAME).write_text("\n".join(rows) + "\n", encoding="utf-8")


def _hour(s, day):
    return s[(s.index >= f"{day} 09:00") & (s.index < f"{day} 10:00")]


def test_winter_gap_is_filled_from_rest(tmp_path):
    day = "2026-11-03"
    _write(tmp_path, day, "09:00", "10:00")          # 웹소켓: DNAS ~08:59, RBAQ 10:00~
    _rest(tmp_path, day, "09:00", "10:00")
    s = _load(tmp_path, day)
    h = _hour(s, day)
    assert len(h) == 60 and h.index.is_monotonic_increasing and h.index.is_unique
    assert h.iloc[0] == pytest.approx(500.5) and h.iloc[-1] == pytest.approx(530.0)
    assert s.index.to_series().diff().max() == pd.Timedelta(minutes=1)          # 08:00~11:00 끊김 없음
    now = datetime(2026, 11, 3, 9, 30, 5)
    live = s[s.index + pd.Timedelta(minutes=1) <= now]
    assert mu_assist._status(live, datetime(2026, 11, 3, 9, 29)) == mu_assist.STATUS_OK   # 이전엔 STALE


def test_websocket_wins_on_overlap_and_rest_outside_window_ignored(tmp_path):
    day = "2026-11-03"
    _write(tmp_path, day, "09:01", "10:00")          # 웹소켓 DNAS 가 09:00 분까지 있음
    _rest(tmp_path, day, "08:55", "10:05")           # REST 가 08:55~10:04 를 줘도
    ws = mu_assist.load_mu_1m(datetime(2026, 11, 3, 16), data_dir=str(tmp_path))
    base = tmp_path / "base"
    _write(base, day, "09:01", "10:00")
    only_ws = _load(base, day)
    # 08:55~09:00 은 웹소켓 값, 10:00~10:04 는 RBAQ 값 그대로 — REST 는 09:01~09:59 만
    for hm in ("08:55", "08:59", "09:00", "10:00", "10:04"):
        assert ws.loc[pd.Timestamp(f"{day} {hm}")] == only_ws.loc[pd.Timestamp(f"{day} {hm}")]
    assert ws.loc[pd.Timestamp(f"{day} 09:01")] == pytest.approx(500.0 + 0.5 * 7)
    assert len(_hour(ws, day)) == 60


@pytest.mark.parametrize("day", ["2026-10-28", "2026-10-26"])        # EDT 평일 / EDT 월요일
def test_summer_rest_file_has_no_effect(tmp_path, day):
    _write(tmp_path, day, "09:00", "09:00")
    base = _load(tmp_path, day)
    _rest(tmp_path, day, "09:00", "10:00")
    after = _load(tmp_path, day)
    pd.testing.assert_series_equal(base, after)


def test_no_file_or_disabled_is_parity(tmp_path, monkeypatch):
    day = "2026-11-03"
    _write(tmp_path, day, "09:00", "10:00")
    base = _load(tmp_path, day)
    _rest(tmp_path, day, "09:00", "10:00")
    monkeypatch.setattr(config, "MU_REST_FALLBACK_ENABLED", False)
    pd.testing.assert_series_equal(base, _load(tmp_path, day))
    assert base.attrs["last_real_minute"] == _load(tmp_path, day).attrs["last_real_minute"]


def test_incomplete_or_foreign_rows_ignored(tmp_path):
    day = "2026-11-03"
    _write(tmp_path, day, "09:00", "10:00")
    _rest(tmp_path, day, "09:00", "09:30",
          extra=[f"{day}T09:30:00,{config.MU_REST_FEED},1,1,1,777,1,0,,,,,0",       # 미확정
                 f"{day}T09:31:00,DNASMU,1,1,1,888,1,0,,,,,1",                       # 다른 feed
                 f"{day}T09:32:00,{config.MU_REST_FEED},1,1,1,bad,1,0,,,,,1"])     # 가격 오류
    h = _hour(_load(tmp_path, day), day)
    assert h.index.max() == pd.Timestamp(f"{day} 09:29") and len(h) == 30
