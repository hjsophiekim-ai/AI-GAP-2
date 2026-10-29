"""MU 분봉 feed 전환 · 서머타임 (2026-10-29).

  · DNASMU(나스닥 애프터마켓) → RBAQMU(블루오션 주간거래) 전환 = 미국 전일 20:00 ET → KST
    서머타임 09:00 / 비서머타임 10:00 (이전엔 09:00 고정이라 겨울에 09:00~09:59 를 버렸다)
  · 가짜 가격 금지: 3분 이하 수신 공백만 직전값으로 잇고 더 긴 공백은 비워 둔다 → stale 이 그대로 드러난다
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import config, mu_assist, mu_catchup
from app.trading.macd2.models import Direction


@pytest.mark.parametrize("day, hhmm", [
    ("2026-10-28", "09:00"),     # EDT
    ("2026-10-30", "09:00"),     # 금 (미국 목 20:00 EDT)
    ("2026-11-02", "10:00"),     # 월 (미국 일 11/1 20:00 EST — DST 종료 당일)
    ("2026-11-03", "10:00"),     # EST
    ("2027-03-15", "09:00"),     # 미국 3/14 DST 시작 당일 20:00 EDT
])
def test_daytime_open_follows_us_dst(day, hhmm):
    assert mu_assist.daytime_open_kst(datetime.strptime(day, "%Y-%m-%d")).strftime("%H:%M") == hhmm


def _write(root: Path, day: str, dnas_until: str, rbaq_from: str, *, skip=()):
    d = root / "mu" / day
    d.mkdir(parents=True, exist_ok=True)
    rows = ["minute_kst,feed,close,complete"]
    t = datetime.strptime(f"{day} 08:00", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{day} 11:00", "%Y-%m-%d %H:%M")
    px = 900.0
    while t <= end:
        px += 0.1
        hm = t.strftime("%H:%M")
        if hm not in skip:
            if hm < dnas_until:
                rows.append(f"{t.isoformat()},DNASMU,{px:.2f},1")
            if hm >= rbaq_from:
                rows.append(f"{t.isoformat()},RBAQMU,{px:.2f},1")
        t += timedelta(minutes=1)
    (d / "bars_1m.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _load(root, day):
    return mu_assist.load_mu_1m(datetime.strptime(day, "%Y-%m-%d").replace(hour=16), data_dir=str(root))


def test_summer_unchanged_continuous(tmp_path):
    _write(tmp_path, "2026-10-28", "09:00", "09:00")
    s = _load(tmp_path, "2026-10-28")
    assert len(s) == 181 and s.index.to_series().diff().max() == pd.Timedelta(minutes=1)


def test_winter_uses_dnas_until_1000(tmp_path):
    """DNAS 가 20:00 EST(10:00 KST)까지 오면 09:00~09:59 를 버리지도, 08:59 값으로 채우지도 않는다."""
    _write(tmp_path, "2026-11-03", "10:00", "10:00")
    s = _load(tmp_path, "2026-11-03")
    assert s.index.to_series().diff().max() == pd.Timedelta(minutes=1)
    assert s.loc[pd.Timestamp("2026-11-03 09:30")] == pytest.approx(900.0 + 0.1 * 91)    # 실제 09:30 값
    assert (s.loc["2026-11-03 09:00":"2026-11-03 09:59"].diff().dropna() != 0).all(), "동일가 가짜 연속 없음"


def test_winter_missing_hour_is_not_synthesised_and_is_stale(tmp_path):
    """DNAS 가 09:00 에 끊기고 RBAQ 가 10:00 에 시작하면 그 60분은 비어 있고, 그 사이 판정은 stale."""
    _write(tmp_path, "2026-11-03", "09:00", "10:00")
    s = _load(tmp_path, "2026-11-03")
    assert not ((s.index >= "2026-11-03 09:00") & (s.index < "2026-11-03 10:00")).any()
    assert s.attrs["last_real_minute"] == pd.Timestamp("2026-11-03 11:00")
    now = datetime(2026, 11, 3, 9, 30, 5, tzinfo=config.KST)
    live = s[s.index + pd.Timedelta(minutes=1) <= now.replace(tzinfo=None)]
    assert mu_assist._status(live, datetime(2026, 11, 3, 9, 29)) == mu_assist.STATUS_STALE
    hy = pd.DataFrame({"datetime": pd.date_range("2026-11-03 08:00", "2026-11-03 09:29", freq="1min", tz=config.KST),
                       "close": 1_000_000.0})
    ev = mu_catchup.evaluate(Direction.UP_RED, now - timedelta(minutes=6), now, hy, mu_series=live,
                             cttr={"cttr": None, "state": "UNAVAILABLE"})
    assert ev["status"] == mu_catchup.UNAVAILABLE and ev["reason"] == "MU_STALE"


@pytest.mark.parametrize("gap, filled", [(("09:10",), True), (("09:10", "09:11", "09:12"), True),
                                         (("09:10", "09:11", "09:12", "09:13"), False)])
def test_only_short_gaps_are_bridged(tmp_path, gap, filled):
    _write(tmp_path, "2026-10-28", "09:00", "09:00", skip=gap)
    s = _load(tmp_path, "2026-10-28")
    present = [pd.Timestamp(f"2026-10-28 {g}") in s.index for g in gap]
    assert all(present) is filled and (any(present) is filled)
