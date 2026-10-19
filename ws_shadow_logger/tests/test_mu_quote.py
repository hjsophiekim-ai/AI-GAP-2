"""MU 해외 실시간호가(HDFSASP0) 수집 테스트 (2026-10-19).

검증 항목
  1) 공식 16필드 schema + 실제 17필드(RSYM) 둘 다 밀림 없이 파싱된다
  2) RBAQMU / DNASMU 가 섞이지 않고 분리된다
  3) kst_ts 가 KST(KYMD/KHMS)다 — 현지(ET) 시각이 아니다
  4) 1호가 bid/ask/잔량이 그대로 저장된다 (**10호가가 아니다**)
  5) spread / top-of-book imbalance 계산이 맞다
  6) 기존 MU 체결(HDFSCNT0) 수집에 회귀가 없다
  7) 국내(하이닉스/ETF) 스냅샷에 회귀가 없다
"""
from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime, timedelta

from ws_shadow_logger import mu_quote, runner, stub
from ws_shadow_logger.parser import parse_frame
from ws_shadow_logger.schema import ALT_COLUMNS, COLUMNS, KIND, OVRS_ASP, OVRS_ASP_OFFICIAL

DAY = "2026-10-19"
KST_OPEN = datetime(2026, 10, 19, 9, 0, 0)
KST_PM = datetime(2026, 10, 19, 13, 30, 0)


def _read(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── 1) schema ──────────────────────────────────────────────────────────────
def test_official_16_field_schema():
    """공식 예제(overseas_stock_functions_ws.asking_price / examples_llm asking_price)의 columns 그대로."""
    assert OVRS_ASP_OFFICIAL == ["SYMB", "ZDIV", "XYMD", "XHMS", "KYMD", "KHMS", "BVOL", "AVOL",
                                 "BDVL", "ADVL", "PBID1", "PASK1", "VBID1", "VASK1", "DBID1", "DASK1"]
    assert len(OVRS_ASP_OFFICIAL) == 16
    # 실제 전문은 RSYM 이 1번 필드 — HDFSCNT0 과 같은 구조(2026-10-18 사고). 기본은 17.
    assert OVRS_ASP[0] == "RSYM" and OVRS_ASP[1:] == OVRS_ASP_OFFICIAL and len(OVRS_ASP) == 17
    assert COLUMNS["HDFSASP0"] is OVRS_ASP and ALT_COLUMNS["HDFSASP0"] is OVRS_ASP_OFFICIAL
    assert KIND["HDFSASP0"] == "us_book"


def test_parses_both_widths_without_drift():
    """17필드(RSYM 있음)와 16필드(공식 예제 포맷) 모두, 건수>1 에서도 레코드가 밀리지 않는다."""
    for with_rsym, width in ((True, 17), (False, 16)):
        m = parse_frame(stub.mu_quote("DNASMU", KST_PM, 120.10, 120.15, 30, 10,
                                      recs=4, with_rsym=with_rsym))
        assert m["width"] == width and len(m["records"]) == 4
        for i, r in enumerate(m["records"]):
            assert r["SYMB"] == "MU", (with_rsym, i)
            assert r["PBID1"] == "120.1" and r["PASK1"] == "120.15", (with_rsym, i)
            assert r["VBID1"] == "30" and r["VASK1"] == "10", (with_rsym, i)
            assert r["KYMD"] == "20261019" and r["KHMS"] == "133000", (with_rsym, i)
            assert r.get("RSYM") == ("DNASMU" if with_rsym else None), (with_rsym, i)


def test_us_book_is_one_level_not_ten():
    """미국주식 호가는 1호가뿐 — 국내 H0STASP0 의 10단 컬럼은 존재하지 않는다."""
    assert not [c for c in OVRS_ASP if c.endswith(("2", "3", "4", "5", "6", "7", "8", "9", "10"))]
    assert "ASKP5" in COLUMNS["H0STASP0"] and "ASKP5" not in OVRS_ASP


# ── 5) spread / imbalance ──────────────────────────────────────────────────
def test_top_of_book_math():
    t = mu_quote.top_of_book("120.10", "120.15", "30", "10")
    assert t["spread"] == 0.05
    assert t["spread_bp"] == round(0.05 / 120.125 * 10000, 4)
    assert t["imb1"] == 0.5                                   # (30-10)/(30+10)
    assert mu_quote.top_of_book("1", "2", "7", "7")["imb1"] == 0.0
    # 값이 없거나 0 이면 조용히 0 으로 만들지 않고 빈칸(None)으로 둔다
    assert mu_quote.top_of_book("0", "120.15", "1", "1")["spread"] is None
    assert mu_quote.top_of_book("1", "2", "0", "0")["imb1"] is None
    assert mu_quote.top_of_book(None, None, None, None) == {"spread": None, "spread_bp": None, "imb1": None}


# ── 2)(3)(4) 저장 ──────────────────────────────────────────────────────────
def _replay_quotes(tmp_path, frames):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "f.txt"
    stub.write(src, frames)
    root = tmp_path / "out"
    runner.replay(str(src), root=root)
    return root


def test_feeds_are_separated_and_fields_stored(tmp_path):
    frames = []
    for i in range(4):
        t = KST_OPEN + timedelta(seconds=10 * i)
        frames.append((t + timedelta(milliseconds=200),
                       stub.mu_quote("RBAQMU", t, 120.10, 120.15, 30, 10, recs=2)))
    for i in range(4):
        t = KST_PM + timedelta(seconds=10 * i)
        frames.append((t + timedelta(milliseconds=200),
                       stub.mu_quote("DNASMU", t, 99.90, 100.10, 5, 15, recs=2)))
    root = _replay_quotes(tmp_path, frames)
    rows = _read(root / "mu" / DAY / "quotes.csv")
    assert len(rows) == 16                                     # 8 프레임 x 2 레코드, 하나도 안 버려진다
    feeds = {r["feed"] for r in rows}
    assert feeds == {"RBAQMU", "DNASMU"}, feeds

    r = next(x for x in rows if x["feed"] == "RBAQMU")
    # 3) KST — 현지(ET)는 13시간 전이고 local_* 칸에만 있다
    assert r["kst_ts"].startswith(f"{DAY}T09:")
    assert r["local_hms"].startswith("20") and r["local_ymd"] == "20261018"
    # 4) 1호가 bid/ask/잔량
    assert (r["pbid1"], r["pask1"], r["vbid1"], r["vask1"]) == ("120.1", "120.15", "30", "10")
    assert r["symbol"] == "MU"
    # 5) 파생값
    assert float(r["spread"]) == 0.05 and float(r["imb1"]) == 0.5
    assert float(r["lag_s"]) == 0.2

    b = next(x for x in rows if x["feed"] == "DNASMU")
    assert b["kst_ts"].startswith(f"{DAY}T13:")
    assert float(b["imb1"]) == -0.5 and round(float(b["spread"]), 2) == 0.2


def test_no_rsym_frame_is_not_merged_into_a_real_feed(tmp_path):
    """RSYM 이 없으면 feed 를 알 수 없다 — RBAQMU/DNASMU 중 하나로 찍지 않고 UNKNOWN 으로 센다."""
    t = KST_PM
    root = _replay_quotes(tmp_path, [(t + timedelta(milliseconds=100),
                                      stub.mu_quote("RBAQMU", t, 1.0, 1.1, 2, 3, with_rsym=False))])
    rows = _read(root / "mu" / DAY / "quotes.csv")
    assert len(rows) == 1 and rows[0]["feed"] == "UNKNOWN_MU"
    assert rows[0]["kst_ts"].startswith(f"{DAY}T13:30")


def test_finalize_day_gzips_and_summarizes(tmp_path):
    frames = [(KST_OPEN + timedelta(minutes=m, milliseconds=100),
               stub.mu_quote("RBAQMU", KST_OPEN + timedelta(minutes=m), 10.0, 10.1, 4, 4))
              for m in (0, 1, 3)]                              # 09:02 는 비어 있다 -> 누락 1분
    root = _replay_quotes(tmp_path, frames)
    s = mu_quote.finalize_day(root, DAY)
    d = root / "mu" / DAY
    assert not (d / "quotes.csv").exists() and (d / "quotes.csv.gz").exists()
    with gzip.open(d / "quotes.csv.gz", "rt", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 3
    assert s == json.loads((d / "quote_summary.json").read_text(encoding="utf-8"))
    f = s["feeds"]["RBAQMU"]
    assert f["rows"] == 3 and f["minutes"] == 3 and f["span_minutes"] == 4
    assert f["missing_minutes"] == 1 and f["missing_ratio"] == 0.25
    assert mu_quote.finalize_day(root, DAY) is None            # 두 번 돌려도 안전


# ── 6) 체결 회귀 0 ─────────────────────────────────────────────────────────
def _trade_frames():
    out = []
    for i in range(6):
        t = KST_OPEN + timedelta(seconds=10 * i)
        out.append((t + timedelta(milliseconds=200), stub.mu_trade("RBAQMU", t, 100.0 + i, 5, 100 + i, recs=2)))
    for i in range(6):
        t = KST_PM + timedelta(seconds=10 * i)
        out.append((t + timedelta(milliseconds=200), stub.mu_trade("DNASMU", t, 200.0 + i, 5, 300 + i, recs=2)))
    return out


def test_mu_trade_path_unchanged_when_quotes_are_mixed_in(tmp_path):
    """같은 스트림에 호가를 섞어도 ticks.csv / bars_1m.csv 가 **바이트까지 같다**."""
    trades = _trade_frames()
    quotes = [(t + timedelta(milliseconds=50), stub.mu_quote("RBAQMU", t, 10.0, 10.2, 3, 1))
              for t, _ in trades[:6]]
    a = _replay_quotes(tmp_path / "a", trades)
    b = _replay_quotes(tmp_path / "b", sorted(trades + quotes, key=lambda x: x[0]))
    for name in ("ticks.csv", "bars_1m.csv"):
        assert (a / "mu" / DAY / name).read_bytes() == (b / "mu" / DAY / name).read_bytes(), name
    assert (b / "mu" / DAY / "quotes.csv").exists()            # 호가는 따로 쌓인다


# ── 7) 국내 회귀 0 ─────────────────────────────────────────────────────────
def test_domestic_snapshots_unchanged_when_quotes_are_mixed_in(tmp_path):
    base = stub.session(datetime(2026, 10, 19, 9, 0, 0), seconds=20, seed=3)
    quotes = [(t + timedelta(milliseconds=10), stub.mu_quote("DNASMU", t, 50.0, 50.1, 9, 1))
              for t, _ in base[::7]]
    a = _replay_quotes(tmp_path / "a", base)
    b = _replay_quotes(tmp_path / "b", sorted(base + quotes, key=lambda x: x[0]))
    for sym in ("000660", "0193T0", "0197X0"):
        pa, pb = a / "snapshots" / DAY / f"{sym}.csv", b / "snapshots" / DAY / f"{sym}.csv"
        assert pa.read_bytes() == pb.read_bytes(), sym
    assert (b / "mu" / DAY / "quotes.csv").exists()


def test_quote_errors_do_not_stop_domestic_or_mu_trade(tmp_path):
    """호가 기록이 죽어도(디스크 오류 등) 국내·체결 수집은 계속된다 — Engine 이 따로 격리한다."""
    from ws_shadow_logger.engine import Engine
    from ws_shadow_logger.storage import Store

    class Boom:
        def on_records(self, *_):
            raise OSError("disk")

        def flush(self):
            pass

        def close(self):
            pass

    root = tmp_path / "out"
    store = Store(root)
    eng = Engine(store, ["000660"], mu_quote=Boom())
    t = KST_OPEN
    eng.on_frame(stub.mu_quote("RBAQMU", t, 1.0, 1.1, 1, 1), t + timedelta(milliseconds=100))
    eng.on_frame(stub.mu_trade("RBAQMU", t, 100.0, 5, 50), t + timedelta(milliseconds=200))
    eng.on_frame(stub.trade("H0UNCNT0", "000660", t, 1_400_000, 10, "1", 100.0, 10, 5, 15),
                 t + timedelta(milliseconds=300))
    eng.advance(t + timedelta(seconds=2))
    eng.close()
    store.close()
    assert eng.mu_quote_errors == 1 and eng.mu_errors == 0 and not eng.mu_disabled
    assert _read(root / "mu" / DAY / "ticks.csv")              # 체결은 그대로 쌓였다
    assert _read(root / "snapshots" / DAY / "000660.csv")      # 국내도 그대로
