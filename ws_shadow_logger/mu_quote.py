"""MU 해외 실시간호가(HDFSASP0) 기록 — 날짜별로 KIS_LOGGER_DATA_DIR/mu/ 아래 (2026-10-19).

  mu/YYYY-MM-DD/quotes.csv            호가 갱신 1건 = 1행, 받는 즉시 append (원문은 raw/ 에도 남는다)
  mu/YYYY-MM-DD/quotes.csv.gz         15:30 이후 압축본
  mu/YYYY-MM-DD/quote_summary.json    feed 별 행 수·구간·분 누락률·RSYM 없는 행 수

**미국주식 호가는 1호가뿐이다.** 국내 H0STASP0 의 10단(ASKP1..10 / BIDP_RSQN1..10)과 다르다 —
여기서 만드는 imbalance 는 최우선 1호가 잔량 기준(top-of-book)이고, 국내 스냅샷의 imb_5/imb_10
같은 깊이 기반 값은 MU 에서 만들 수 없다.

체결(HDFSCNT0, mu.py)과 파일도 코드경로도 따로다. 이 모듈의 어떤 예외도 체결 기록이나 국내
수집을 멈추지 않는다 — Engine 이 호출을 try 로 감싸고 따로 센다.
메모리: feed 별 카운터와 파일 핸들 1개뿐. 행은 쓰고 버린다.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import shutil
from collections import deque
from datetime import datetime
from pathlib import Path

from .mu import kst_ts, mu_dir
from .parser import num
from .storage import day_str

QUOTE_COLUMNS = ["recv_kst", "feed", "symbol", "kst_ts", "local_ymd", "local_hms",
                 "pbid1", "pask1", "vbid1", "vask1", "spread", "spread_bp", "imb1",
                 "bvol", "avol", "lag_s"]

#: RSYM 이 없는 전문(공식 예제 16필드 포맷)에서 feed 를 구분할 수 없을 때 쓰는 표식.
UNKNOWN_FEED = "UNKNOWN"


def top_of_book(pbid, pask, vbid, vask) -> dict:
    """1호가 스프레드와 잔량 imbalance. imb1 정의는 국내 스냅샷 aggregator 의 imb_1 과 같다
    — (매수잔량 - 매도잔량) / (매수잔량 + 매도잔량). 값이 없으면 None(빈칸)이다."""
    pb, pa = num(pbid), num(pask)
    vb, va = num(vbid), num(vask)
    spread = spread_bp = imb1 = None
    if pb is not None and pa is not None and pb > 0 and pa > 0:
        spread = round(pa - pb, 6)
        mid = (pa + pb) / 2
        if mid:
            spread_bp = round(spread / mid * 10000, 4)
    if vb is not None and va is not None and (vb + va) > 0:
        imb1 = round((vb - va) / (vb + va), 6)
    return {"spread": spread, "spread_bp": spread_bp, "imb1": imb1}


def feed_of(rec: dict) -> str:
    """feed = RSYM (RBAQMU 주간거래 / DNASMU 나스닥). SYMB 는 두 피드 모두 MU 라 구분에 못 쓴다
    (HDFSCNT0 에서 실측, 2026-10-18). RSYM 이 없는 16필드 전문이면 구분이 불가능하므로
    조용히 합치지 않고 UNKNOWN 으로 따로 센다."""
    rsym = str(rec.get("RSYM") or "").strip()
    if rsym:
        return rsym
    symb = str(rec.get("SYMB") or "").strip()
    return f"{UNKNOWN_FEED}_{symb}" if symb else UNKNOWN_FEED


class MuQuoteRecorder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._day = None
        self._quotes = None             # (fh, writer)
        self.counts = {"quotes": 0, "no_rsym": 0}
        self.last_quote_at: datetime | None = None
        self.feed_rows: dict = {}       # feed -> 행 수 (이번 프로세스 기준)
        self.feed_minutes: dict = {}    # feed -> [첫 분, 마지막 분]
        self.lags = deque(maxlen=600)

    # -- 파일 ---------------------------------------------------------------
    def _open(self, day: str):
        if day == self._day:
            return
        self.close()
        d = mu_dir(self.root, day)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "quotes.csv"
        new = not p.exists() or p.stat().st_size == 0
        fh = open(p, "a", newline="", encoding="utf-8")
        w = csv.DictWriter(fh, fieldnames=QUOTE_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        self._quotes = (fh, w)
        self._day = day

    # -- 입력 ---------------------------------------------------------------
    def on_records(self, recs, recv: datetime):
        for rec in recs:
            kt = kst_ts(rec)
            if kt is None:
                continue
            feed = feed_of(rec)
            if feed.startswith(UNKNOWN_FEED):
                self.counts["no_rsym"] += 1
            self._open(day_str(kt))
            tob = top_of_book(rec.get("PBID1"), rec.get("PASK1"), rec.get("VBID1"), rec.get("VASK1"))
            row = {"recv_kst": recv.isoformat(timespec="milliseconds"), "feed": feed,
                   "symbol": rec.get("SYMB"), "kst_ts": kt.isoformat(timespec="seconds"),
                   "local_ymd": rec.get("XYMD"), "local_hms": rec.get("XHMS"),
                   "pbid1": rec.get("PBID1"), "pask1": rec.get("PASK1"),
                   "vbid1": rec.get("VBID1"), "vask1": rec.get("VASK1"),
                   "spread": tob["spread"], "spread_bp": tob["spread_bp"], "imb1": tob["imb1"],
                   "bvol": rec.get("BVOL"), "avol": rec.get("AVOL"),
                   "lag_s": round((recv - kt).total_seconds(), 3)}
            self._quotes[1].writerow(row)
            self.counts["quotes"] += 1
            self.feed_rows[feed] = self.feed_rows.get(feed, 0) + 1
            minute = row["kst_ts"][:16]
            span = self.feed_minutes.get(feed)
            if span is None:
                self.feed_minutes[feed] = [minute, minute]
            else:
                span[0] = min(span[0], minute)
                span[1] = max(span[1], minute)
            self.lags.append(row["lag_s"])
            self.last_quote_at = recv

    def observe(self) -> dict:
        """health 용 요약 — feed 별 행 수/구간(이번 프로세스 기준), 최근 600건 수신지연."""
        feeds = {}
        for f in sorted(self.feed_rows):
            first, last = self.feed_minutes.get(f, [None, None])
            feeds[f] = {"rows": self.feed_rows[f], "first": first, "last": last}
        lags = sorted(self.lags)
        lag = ({"p50": lags[len(lags) // 2], "p95": lags[min(len(lags) - 1, int(len(lags) * 0.95))],
                "max": lags[-1], "n": len(lags)} if lags else None)
        return {"feeds": feeds, "lag": lag, "no_rsym": self.counts["no_rsym"]}

    def flush(self):
        if self._quotes:
            self._quotes[0].flush()

    def close(self):
        if self._quotes:
            self._quotes[0].close()
        self._quotes = None
        self._day = None


def _iter_quotes(d: Path):
    for p, op in ((d / "quotes.csv.gz", gzip.open), (d / "quotes.csv", open)):
        if p.exists():
            with op(p, "rt", newline="", encoding="utf-8") as fh:
                yield from csv.DictReader(fh)


def summarize_quotes(rows) -> dict:
    """feed 별 행 수 / 첫·마지막 분 / 호가 갱신이 한 번도 없는 분의 비율. 한 줄씩 받아 센다."""
    by: dict = {}
    for r in rows:
        f = r.get("feed") or "?"
        t = (r.get("kst_ts") or "")[:16]
        if len(t) < 16:
            continue
        d = by.setdefault(f, {"rows": 0, "minutes": set()})
        d["rows"] += 1
        d["minutes"].add(t)
    out = {}
    for f, d in by.items():
        mins = sorted(d["minutes"])
        t0, t1 = datetime.fromisoformat(mins[0] + ":00"), datetime.fromisoformat(mins[-1] + ":00")
        span = int((t1 - t0).total_seconds() // 60) + 1
        out[f] = {"rows": d["rows"], "minutes": len(mins), "first": mins[0], "last": mins[-1],
                  "span_minutes": span, "missing_minutes": span - len(mins),
                  "missing_ratio": round((span - len(mins)) / span, 4)}
    return out


def finalize_day(root: Path, day: str) -> dict | None:
    """장 마감 후 한 번: quotes.csv -> quotes.csv.gz + quote_summary.json.
    이미 끝난 날(quotes.csv 없음)이면 None. mu.finalize_day 와 같은 방식이다."""
    d = mu_dir(root, day)
    qp = d / "quotes.csv"
    if not qp.exists():
        return None
    summary = {"day": day, "feeds": summarize_quotes(_iter_quotes(d))}
    summary["quotes"] = sum(v["rows"] for v in summary["feeds"].values())
    gz = d / "quotes.csv.gz"
    part = d / "quotes.csv.gz.part"
    with open(qp, "rb") as fi, gzip.open(part, "wb") as fo:
        if gz.exists():
            fi.readline()                       # 이어 붙일 때 헤더는 한 번만
        shutil.copyfileobj(fi, fo, 64 * 1024)
    with open(gz, "ab") as out, open(part, "rb") as src:
        shutil.copyfileobj(src, out, 64 * 1024)
    part.unlink()
    qp.unlink()
    tmp = d / "quote_summary.tmp"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, d / "quote_summary.json")
    return summary
