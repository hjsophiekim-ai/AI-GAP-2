"""HDFSCNT0 RSYM 결함 회귀테스트 (2026-10-18).

2026-10-16 R2 실측에서 드러난 결함:
  schema.OVRS_CCNL 이 RSYM(1번 필드)을 빠뜨려 폭이 25 였다. 한 프레임에 레코드가 여러 개면
  레코드마다 1칸씩 밀려서
    · SYMB 에 가격/체결강도가 들어가 feed 가 "999.99","100.00" 같은 쓰레기가 되고
    · KYMD/KHMS 에 현지(ET) 일자·시각이 들어가 분봉이 KST 가 아닌 ET 로 찍혔다
      (실측: 수신 13:00~15:29 KST 인데 분봉은 00:00~02:29 로 기록 = 정확히 13시간)
    · 레코드 0 은 KYMD 자리에 6자리 XHMS 가 들어가 kst_ts=None 으로 통째로 버려졌다
여기서는 **공식 26필드 프레임**으로 위 넷이 모두 올바른지 확인한다.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta

from ws_shadow_logger import mu, runner, stub
from ws_shadow_logger.parser import parse_frame
from ws_shadow_logger.schema import OVRS_CCNL

DAY = "2026-10-16"
KST_OPEN = datetime(2026, 10, 16, 9, 0, 0)        # 한국 장 시작
KST_PM = datetime(2026, 10, 16, 13, 30, 0)


def _read(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_schema_has_rsym_and_width_26():
    assert OVRS_CCNL[0] == "RSYM" and OVRS_CCNL[1] == "SYMB" and len(OVRS_CCNL) == 26


def test_multi_record_frame_does_not_drift():
    """건수>1 프레임의 **모든** 레코드가 같은 값으로 파싱된다 (드리프트 없음)."""
    m = parse_frame(stub.mu_trade("DNASMU", KST_PM, 123.45, 7, 700, recs=5))
    assert len(m["records"]) == 5
    for i, r in enumerate(m["records"]):
        assert r["RSYM"] == "DNASMU", i
        assert r["SYMB"] == "MU", i
        assert r["LAST"] == "123.45", i
        assert r["KYMD"] == "20261016" and r["KHMS"] == "133000", i
        # 현지(ET) 값은 현지 칸에만 있어야 한다 — KST 칸을 침범하지 않는다
        assert r["XHMS"] == (KST_PM - timedelta(hours=13)).strftime("%H%M%S"), i


def test_kst_timestamp_is_kst_not_et():
    r = parse_frame(stub.mu_trade("DNASMU", KST_PM, 10.0, 1, 1, recs=3))["records"][2]
    kt = mu.kst_ts(r)
    assert kt == KST_PM                                   # ET(00:30) 가 아니라 KST(13:30)
    assert kt.hour == 13 and (kt - datetime.strptime(r["XYMD"] + r["XHMS"], "%Y%m%d%H%M%S")).total_seconds() == 13 * 3600


def test_two_feeds_are_not_merged(tmp_path):
    """RBAQMU(주간거래)와 DNASMU(나스닥)가 각각의 분봉을 만든다. SYMB 는 둘 다 'MU' 라
    그걸 feed 로 쓰면 하나로 합쳐진다."""
    frames = []
    for i in range(6):
        t = KST_OPEN + timedelta(seconds=10 * i)
        frames.append((t + timedelta(milliseconds=200), stub.mu_trade("RBAQMU", t, 100.0 + i, 5, 100, recs=2)))
    for i in range(6):
        t = KST_PM + timedelta(seconds=10 * i)
        frames.append((t + timedelta(milliseconds=200), stub.mu_trade("DNASMU", t, 200.0 + i, 5, 100, recs=2)))
    src = tmp_path / "f.txt"
    stub.write(src, frames)
    root = tmp_path / "out"
    runner.replay(str(src), root=root)
    ticks = _read(root / "mu" / DAY / "ticks.csv")
    feeds = {r["feed"] for r in ticks}
    assert feeds == {"RBAQMU", "DNASMU"}, feeds
    bars = _read(root / "mu" / DAY / "bars_1m.csv")
    by = {}
    for b in bars:
        by.setdefault(b["feed"], []).append(b["minute_kst"])
    assert set(by) == {"RBAQMU", "DNASMU"}
    # 한국 장중(09:00~15:30 KST) 분에 찍힌다 — ET 로 찍히면 00:xx 가 된다
    assert all(m.startswith(f"{DAY}T09:") for m in by["RBAQMU"]), by["RBAQMU"]
    assert all(m.startswith(f"{DAY}T13:") for m in by["DNASMU"]), by["DNASMU"]


def test_record_zero_is_not_dropped(tmp_path):
    """레코드 0 도 기록된다(기존에는 KYMD 자리에 6자리 시각이 들어가 통째로 버려졌다)."""
    t = KST_PM
    src = tmp_path / "f.txt"
    stub.write(src, [(t + timedelta(milliseconds=100), stub.mu_trade("DNASMU", t, 55.5, 3, 30, recs=1))])
    root = tmp_path / "out"
    runner.replay(str(src), root=root)
    ticks = _read(root / "mu" / DAY / "ticks.csv")
    assert len(ticks) == 1 and ticks[0]["feed"] == "DNASMU" and ticks[0]["last"] == "55.5"
    assert ticks[0]["kst_ts"].startswith(f"{DAY}T13:30")
