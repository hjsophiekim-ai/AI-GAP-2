"""저장 구조 — 전부 KIS_LOGGER_DATA_DIR 아래 (Render Persistent Disk).

  raw/YYYY-MM-DD/frames_HH.txt        수신 원문 그대로 "<수신 ISO ms>\t<frame>" — 장중엔 **무압축 즉시 append**
  raw/YYYY-MM-DD/frames_HH.txt.gz     15:30 이후 maintenance 가 위 파일을 gzip 으로 바꾼다
  snapshots/YYYY-MM-DD/<symbol>.csv   1초 스냅샷 (aggregator.SNAPSHOT_COLUMNS), 즉시 append
  logs/YYYY-MM-DD.jsonl               접속/구독응답/끊김/재접속/gap/health/오류 이벤트
  state/approval_YYYYMMDD.json        로거 전용 접속키 캐시
  state/counters_YYYY-MM-DD.json      그 날 raw/snapshot/reconnect 누계 (재시작 시 이어서 센다)
  daily/YYYY-MM-DD.json               15:30 이후 일별 요약

메모리: 프레임/행을 모아 두지 않는다. 받는 즉시 파일에 쓰고, 열린 파일 핸들(종목당 1개 + raw 1개)만 든다.
재시작: 모든 파일은 append 로 이어 쓴다. 스냅샷은 파일 마지막 ts 이하를 다시 쓰지 않는다(중복 방지).
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path

from .aggregator import SNAPSHOT_COLUMNS


def day_str(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def _last_line(p: Path) -> str | None:
    """파일 끝 한 줄만 읽는다(전체를 메모리에 올리지 않는다)."""
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end == 0:
                return None
            pos = max(0, end - 4096)
            fh.seek(pos)
            chunk = fh.read().decode("utf-8", errors="ignore").rstrip("\r\n")
            return chunk.splitlines()[-1] if chunk else None
    except OSError:
        return None


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._raw = None
        self._raw_key = None
        self._snap = {}             # symbol -> (fh, writer, day)
        self._last_ts = {}          # symbol -> 마지막으로 기록된 스냅샷 ts (iso, 같은 날)
        self.counters = {}          # 오늘 누계 (load_counters 로 이어받는다)
        self._counters_day = None

    # ── 일별 누계 ──────────────────────────────────────────────────────────
    def _counters_path(self, day: str) -> Path:
        return self.root / "state" / f"counters_{day}.json"

    def ensure_day(self, ts: datetime):
        day = day_str(ts)
        if day == self._counters_day:
            return
        if self._counters_day is not None:
            self.save_counters()
        p = self._counters_path(day)
        base = {"raw": 0, "snapshot": 0, "reconnect": 0, "gap_events": 0, "max_gap_s": 0.0}
        if p.exists():
            try:
                base.update(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.counters = base
        self._counters_day = day

    def save_counters(self):
        if self._counters_day is None:
            return
        p = self._counters_path(self._counters_day)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.counters), encoding="utf-8")
        os.replace(tmp, p)

    # ── 쓰기 ───────────────────────────────────────────────────────────────
    def raw(self, recv: datetime, frame: str):
        self.ensure_day(recv)
        key = recv.strftime("%Y%m%d_%H")
        if key != self._raw_key:
            if self._raw:
                self._raw.close()
            p = self.root / "raw" / day_str(recv) / f"frames_{recv:%H}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            self._raw = open(p, "a", encoding="utf-8")
            self._raw_key = key
        self._raw.write(f"{recv.isoformat(timespec='milliseconds')}\t{frame}\n")
        self.counters["raw"] += 1

    def snapshot(self, row: dict, ts: datetime):
        self.ensure_day(ts)
        sym = row["symbol"]
        day = day_str(ts)
        cur = self._snap.get(sym)
        if cur is None or cur[2] != day:
            if cur:
                cur[0].close()
            p = self.root / "snapshots" / day / f"{sym}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists() or p.stat().st_size == 0
            last = None if new else _last_line(p)
            self._last_ts[sym] = None if (not last or last.startswith("ts,")) else last.split(",", 1)[0]
            fh = open(p, "a", newline="", encoding="utf-8")
            w = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            cur = (fh, w, day)
            self._snap[sym] = cur
        ts_iso = row["ts"]
        if self._last_ts.get(sym) is not None and ts_iso <= self._last_ts[sym]:
            return                                   # 재시작 직후 같은 초 — 이미 기록돼 있다
        cur[1].writerow(row)
        self._last_ts[sym] = ts_iso
        self.counters["snapshot"] += 1

    def log(self, ts: datetime, event: str, **kw):
        p = self.root / "logs" / f"{day_str(ts)}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts.isoformat(timespec="milliseconds"), "event": event, **kw},
                                ensure_ascii=False, default=str) + "\n")

    # 예전 이름 (engine/replay 가 쓰던 것)
    meta = log

    def flush(self):
        if self._raw:
            self._raw.flush()
        for fh, _, _ in self._snap.values():
            fh.flush()

    def close(self):
        if self._raw:
            self._raw.close()
            self._raw = None
            self._raw_key = None
        for fh, _, _ in self._snap.values():
            fh.close()
        self._snap = {}
        self.save_counters()
