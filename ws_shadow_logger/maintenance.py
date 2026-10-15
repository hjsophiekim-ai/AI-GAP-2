"""장 마감 후(15:30 이후) 정리 — 수집 중에는 절대 돌리지 않는다.

  1) 끝난 날(어제 이전, 또는 15:30 이 지난 오늘)의 raw/YYYY-MM-DD/frames_HH.txt -> .txt.gz
     스트리밍 복사(64KB 청크) 후 원본 삭제. 같은 이름 .gz 가 이미 있으면 gzip 멤버로 이어 붙인다
     (gzip 은 멤버 연결을 그대로 읽는다 — 재시작으로 생긴 조각도 잃지 않는다).
  2) daily/YYYY-MM-DD.json 일별 요약 — 스냅샷 CSV 를 한 줄씩 읽어 센다(pandas 없음).

메모리 사용은 청크 버퍼와 카운터뿐이다.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config
from .storage import day_str

CHUNK = 64 * 1024
STALE_AGE_S = 5.0        # join.MAX_AGE_S 와 같은 기준 — book/trade 나이 > 5초면 stale


def _finished(day: str, now: datetime) -> bool:
    today = day_str(now)
    return day < today or (day == today and now.time() >= config.SESSION_END)


def gzip_raw(root: Path, now: datetime) -> list[str]:
    done = []
    raw_root = Path(root) / "raw"
    if not raw_root.exists():
        return done
    for d in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        if not _finished(d.name, now):
            continue
        for src in sorted(d.glob("frames_*.txt")):
            dst = src.with_name(src.name + ".gz")
            tmp = src.with_name(src.name + ".gz.part")
            with open(src, "rb") as fi, gzip.open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo, CHUNK)
            with open(dst, "ab") as out, open(tmp, "rb") as part:
                shutil.copyfileobj(part, out, CHUNK)
            tmp.unlink()
            src.unlink()
            done.append(str(dst))
    return done


def _summarize_snapshots(p: Path) -> dict:
    n = stale = 0
    first = last = None
    max_gap = 0.0
    prev = None
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            n += 1
            ts = row.get("ts") or ""
            first = first or ts
            last = ts
            try:
                t = datetime.fromisoformat(ts)
                if prev is not None:
                    max_gap = max(max_gap, (t - prev).total_seconds())
                prev = t
            except ValueError:
                pass
            ages = [row.get("book_age_s"), row.get("trade_age_s")]
            vals = [float(a) for a in ages if a not in (None, "")]
            if not vals or min(vals) > STALE_AGE_S:
                stale += 1
    return {"rows": n, "first_ts": first, "last_ts": last, "max_ts_gap_s": max_gap,
            "stale_rows": stale, "stale_ratio": round(stale / n, 4) if n else None}


def daily_summary(root: Path, now: datetime) -> list[str]:
    root = Path(root)
    made = []
    snap_root = root / "snapshots"
    if not snap_root.exists():
        return made
    for d in sorted(p for p in snap_root.iterdir() if p.is_dir()):
        out = root / "daily" / f"{d.name}.json"
        if out.exists() or not _finished(d.name, now):
            continue
        summary = {"day": d.name, "symbols": {}}
        for f in sorted(d.glob("*.csv")):
            summary["symbols"][f.stem] = _summarize_snapshots(f)
        cp = root / "state" / f"counters_{d.name}.json"
        if cp.exists():
            try:
                summary["counters"] = json.loads(cp.read_text(encoding="utf-8"))
            except ValueError:
                pass
        raw_dir = root / "raw" / d.name
        summary["raw_files"] = sorted(x.name for x in raw_dir.iterdir()) if raw_dir.exists() else []
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out)
        made.append(str(out))
    return made


def run(root: Path, now: datetime) -> dict:
    """수집시간이면 아무 것도 하지 않는다(장중 gzip/집계 금지)."""
    if config.SESSION_START <= now.time() < config.SESSION_END and now.weekday() < 5:
        return {"skipped": "in_session"}
    return {"gzipped": gzip_raw(root, now), "daily": daily_summary(root, now)}
