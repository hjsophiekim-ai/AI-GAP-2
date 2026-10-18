"""MU 실시간 체결(HDFSCNT0) 기록 — tick CSV + 1분봉 OHLCV, 날짜별로 KIS_LOGGER_DATA_DIR/mu/ 아래 (2026-10-15).

  mu/YYYY-MM-DD/ticks.csv      체결 1건 = 1행, 받는 즉시 append (원문 프레임은 raw/ 에도 그대로 남는다)
  mu/YYYY-MM-DD/bars_1m.csv    1분봉. 장중엔 확정되는 대로 append, 15:30 이후 maintenance 가 ticks 로 다시 만든다
  mu/YYYY-MM-DD/ticks.csv.gz   15:30 이후 압축된 ticks.csv
  mu/YYYY-MM-DD/summary.json   15:30 이후 feed 별 분봉 수·누락률

시각: 분봉의 기준은 **KST** — 레코드의 KYMD/KHMS(한국 일자/시각)를 쓴다. 하이닉스 스냅샷 ts 도 KST(수신시각)라
같은 분 문자열로 바로 join 된다. 수신시각과의 차이는 ticks.csv 의 lag_s 에 남긴다.
feed: 레코드의 SYMB 값 그대로(RBAQMU 주간거래 / DNASMU 나스닥). 둘을 섞지 않고 분봉도 feed 별로 만든다.
거래량: 분봉 volume 은 EVOL(체결량) 합이다 — 누적 TVOL 은 feed 마다 달라 섞이면 틀리므로 쓰지 않는다.

격리: 이 모듈의 어떤 예외도 국내 수집을 멈추지 않는다. Engine 이 모든 호출을 try 로 감싸고 오류를 센다.
메모리: 들고 있는 것은 feed 별 진행 중인 분봉 1개와 파일 핸들 2개뿐이다.
"""
from __future__ import annotations

import csv
import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from .config import MU_BAR_GRACE_SEC
from .parser import num
from .storage import day_str

TICK_COLUMNS = ["recv_kst", "feed", "kst_ts", "local_ymd", "local_hms", "last", "evol", "tvol",
                "pbid", "pask", "vbid", "vask", "strn", "mtyp", "lag_s"]
BAR_COLUMNS = ["minute_kst", "feed", "open", "high", "low", "close", "volume", "ticks",
               "first_kst", "last_kst", "first_recv", "last_recv", "complete"]


def kst_ts(rec: dict) -> datetime | None:
    d, t = str(rec.get("KYMD") or ""), str(rec.get("KHMS") or "")
    if len(d) != 8 or len(t) < 6:
        return None
    try:
        return datetime.strptime(d + t[:6], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def mu_dir(root: Path, day: str) -> Path:
    return Path(root) / "mu" / day


def _read_last_minutes(p: Path) -> dict:
    """bars_1m.csv 의 feed 별 마지막 분 — 하루 수백 행이라 통째로 읽어도 작다."""
    out = {}
    if not p.exists():
        return out
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            f, m = row.get("feed"), row.get("minute_kst")
            if f and m and m > out.get(f, ""):
                out[f] = m
    return out


def build_bars(ticks) -> list[dict]:
    """tick 행(dict) 반복자 -> feed 별 1분봉. maintenance 재생성과 테스트가 쓴다."""
    bars: dict = {}
    for r in ticks:
        t = r.get("kst_ts") or ""
        px = num(r.get("last"))
        if len(t) < 16 or px is None:
            continue
        key = (r.get("feed") or "", t[:16] + ":00")
        b = bars.get(key)
        vol = num(r.get("evol"), 0.0) or 0.0
        if b is None:
            bars[key] = {"minute_kst": key[1], "feed": key[0], "open": px, "high": px, "low": px, "close": px,
                         "volume": vol, "ticks": 1, "first_kst": t, "last_kst": t,
                         "first_recv": r.get("recv_kst", ""), "last_recv": r.get("recv_kst", ""), "complete": 1}
        else:
            b["high"] = max(b["high"], px); b["low"] = min(b["low"], px)
            if t >= b["last_kst"]:
                b["close"] = px; b["last_kst"] = t; b["last_recv"] = r.get("recv_kst", "")
            b["volume"] += vol; b["ticks"] += 1
    return [bars[k] for k in sorted(bars, key=lambda k: (k[1], k[0]))]


class MuRecorder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._day = None
        self._ticks = None          # (fh, writer)
        self._bars = None
        self._last_min: dict = {}   # feed -> 마지막으로 쓴 minute_kst (재시작 중복 방지)
        self._cur: dict = {}        # feed -> 진행 중 분봉
        self.counts = {"ticks": 0, "bars": 0}
        self.last_tick_at: datetime | None = None
        # 장중 관측용(재시작 후 0부터) — feed 별 tick/분봉 수, 첫·마지막 분, 최근 수신지연
        self.feed_ticks: dict = {}
        self.feed_bars: dict = {}           # feed -> [bars, first_minute, last_minute]
        self.lags = deque(maxlen=600)

    # ── 파일 ───────────────────────────────────────────────────────────────
    def _open(self, day: str):
        if day == self._day:
            return
        self.close()
        d = mu_dir(self.root, day)
        d.mkdir(parents=True, exist_ok=True)
        tp, bp = d / "ticks.csv", d / "bars_1m.csv"
        self._last_min = _read_last_minutes(bp)
        for name, p, cols in (("_ticks", tp, TICK_COLUMNS), ("_bars", bp, BAR_COLUMNS)):
            new = not p.exists() or p.stat().st_size == 0
            fh = open(p, "a", newline="", encoding="utf-8")
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            setattr(self, name, (fh, w))
        self._day = day

    # ── 입력 ───────────────────────────────────────────────────────────────
    def on_records(self, recs, recv: datetime):
        for rec in recs:
            kt = kst_ts(rec)
            px = num(rec.get("LAST"))
            if kt is None or px is None:
                continue
            # feed 는 RSYM(RBAQMU 주간거래 / DNASMU 나스닥). SYMB 는 둘 다 "MU" 라
            # 그걸 쓰면 두 피드가 한 분봉으로 합쳐진다 (2026-10-18 수정).
            feed = str(rec.get("RSYM") or rec.get("SYMB") or "").strip() or "?"
            self._open(day_str(kt))
            row = {"recv_kst": recv.isoformat(timespec="milliseconds"), "feed": feed,
                   "kst_ts": kt.isoformat(timespec="seconds"), "local_ymd": rec.get("XYMD"),
                   "local_hms": rec.get("XHMS"), "last": rec.get("LAST"), "evol": rec.get("EVOL"),
                   "tvol": rec.get("TVOL"), "pbid": rec.get("PBID"), "pask": rec.get("PASK"),
                   "vbid": rec.get("VBID"), "vask": rec.get("VASK"), "strn": rec.get("STRN"),
                   "mtyp": rec.get("MTYP"), "lag_s": round((recv - kt).total_seconds(), 3)}
            self._ticks[1].writerow(row)
            self.counts["ticks"] += 1
            self.feed_ticks[feed] = self.feed_ticks.get(feed, 0) + 1
            self.lags.append(row["lag_s"])
            self.last_tick_at = recv
            self._on_tick(feed, kt, px, num(rec.get("EVOL"), 0.0) or 0.0, row)

    def _on_tick(self, feed, kt: datetime, px: float, vol: float, row: dict):
        minute = kt.replace(second=0).isoformat(timespec="seconds")
        b = self._cur.get(feed)
        if b is not None and minute > b["minute_kst"]:
            self._emit(b, complete=1)
            b = None
        if b is None:
            if minute <= self._last_min.get(feed, ""):
                return                          # 재시작 직후, 이미 쓴 분
            self._cur[feed] = {"minute_kst": minute, "feed": feed, "open": px, "high": px, "low": px, "close": px,
                               "volume": vol, "ticks": 1, "first_kst": row["kst_ts"], "last_kst": row["kst_ts"],
                               "first_recv": row["recv_kst"], "last_recv": row["recv_kst"]}
            return
        if minute < b["minute_kst"]:
            return                              # 늦게 온 이전 분 체결 — tick 은 남았고 maintenance 재생성이 반영한다
        b["high"] = max(b["high"], px); b["low"] = min(b["low"], px); b["close"] = px
        b["volume"] += vol; b["ticks"] += 1
        b["last_kst"] = row["kst_ts"]; b["last_recv"] = row["recv_kst"]

    def advance(self, now: datetime):
        """시계 기준 확정: 분 끝 + grace 가 지난 진행 중 분봉을 쓴다(다음 체결이 안 오는 조용한 분)."""
        for feed, b in list(self._cur.items()):
            end = datetime.fromisoformat(b["minute_kst"]) + timedelta(seconds=60 + MU_BAR_GRACE_SEC)
            if now >= end:
                self._emit(b, complete=1)

    def _emit(self, b: dict, complete: int):
        self._open(b["minute_kst"][:10])
        self._bars[1].writerow({**b, "complete": complete})
        self._last_min[b["feed"]] = b["minute_kst"]
        self._cur.pop(b["feed"], None)
        self.counts["bars"] += 1
        fb = self.feed_bars.setdefault(b["feed"], [0, b["minute_kst"], b["minute_kst"]])
        fb[0] += 1
        fb[1] = min(fb[1], b["minute_kst"]); fb[2] = max(fb[2], b["minute_kst"])

    def observe(self) -> dict:
        """health 용 요약: feed 별 tick 수, 분봉 수/구간/누락률(이번 프로세스 기준), 최근 600건 수신지연."""
        feeds = {}
        for f in sorted(set(self.feed_ticks) | set(self.feed_bars)):
            n, first, last = self.feed_bars.get(f, [0, None, None])
            span = (int((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() // 60) + 1
                    if first else 0)
            feeds[f] = {"ticks": self.feed_ticks.get(f, 0), "bars": n, "span": span,
                        "missing_ratio": round((span - n) / span, 4) if span else None}
        lags = sorted(self.lags)
        lag = ({"p50": lags[len(lags) // 2], "p95": lags[min(len(lags) - 1, int(len(lags) * 0.95))],
                "max": lags[-1], "n": len(lags)} if lags else None)
        return {"feeds": feeds, "lag": lag}

    def flush(self):
        for h in (self._ticks, self._bars):
            if h:
                h[0].flush()

    def close(self):
        """진행 중 분봉은 complete=0 으로 남긴다(재시작/세션 종료). maintenance 가 ticks 로 다시 만든다."""
        for b in list(self._cur.values()):
            if self._bars:
                self._bars[1].writerow({**b, "complete": 0})
                self._last_min[b["feed"]] = b["minute_kst"]
        self._cur = {}
        for name in ("_ticks", "_bars"):
            h = getattr(self, name)
            if h:
                h[0].close()
            setattr(self, name, None)
        self._day = None


def _iter_ticks(d: Path):
    import gzip
    for p, op in ((d / "ticks.csv.gz", gzip.open), (d / "ticks.csv", open)):
        if p.exists():
            with op(p, "rt", newline="", encoding="utf-8") as fh:
                yield from csv.DictReader(fh)


def finalize_day(root: Path, day: str) -> dict | None:
    """장 마감 후 한 번: ticks -> bars_1m.csv 재생성(재시작으로 갈린 분까지 정확), ticks.csv -> ticks.csv.gz,
    feed 별 누락률을 mu/YYYY-MM-DD/summary.json 에 남긴다. 이미 끝난 날(ticks.csv 없음)이면 None."""
    import gzip
    import json
    import shutil
    d = mu_dir(root, day)
    tp = d / "ticks.csv"
    if not tp.exists():
        return None
    bars = build_bars(_iter_ticks(d))
    bp = d / "bars_1m.csv"
    tmp = bp.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BAR_COLUMNS)
        w.writeheader()
        w.writerows(bars)
    os.replace(tmp, bp)
    gz = d / "ticks.csv.gz"
    part = d / "ticks.csv.gz.part"
    with open(tp, "rb") as fi, gzip.open(part, "wb") as fo:
        if gz.exists():
            fi.readline()                       # 이어 붙일 때 헤더는 한 번만
        shutil.copyfileobj(fi, fo, 64 * 1024)
    with open(gz, "ab") as out, open(part, "rb") as src:
        shutil.copyfileobj(src, out, 64 * 1024)
    part.unlink()
    tp.unlink()
    summary = {"day": day, "feeds": summarize_bars(bars), "ticks": sum(b["ticks"] for b in bars)}
    (d / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def summarize_bars(bars) -> dict:
    """feed 별 분봉 수와 누락률. 누락률 = 첫 봉~마지막 봉 사이 체결 없는 분 / 그 구간 분 수.
    (체결이 없는 분은 데이터 유실이 아니라 거래 공백일 수도 있다 — raw 와 logs 의 gap 으로 구분한다.)"""
    out = {}
    by = {}
    for b in bars:
        by.setdefault(b["feed"], []).append(b["minute_kst"])
    for feed, mins in by.items():
        mins = sorted(set(mins))
        t0, t1 = datetime.fromisoformat(mins[0]), datetime.fromisoformat(mins[-1])
        span = int((t1 - t0).total_seconds() // 60) + 1
        out[feed] = {"bars": len(mins), "first": mins[0], "last": mins[-1], "span_minutes": span,
                     "missing_minutes": span - len(mins), "missing_ratio": round((span - len(mins)) / span, 4)}
    return out
