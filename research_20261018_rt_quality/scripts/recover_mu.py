"""과거 MU 데이터 복구 — raw frame 을 **새 schema(26필드)** 로 재파싱 (2026-10-18).

RESEARCH / RECOVERY ONLY.
  · 원본 raw 파일은 **열기만** 한다. 수정·삭제·이동 없음 (읽기 전용 open).
  · production 코드를 import 만 하고 호출로 상태를 바꾸지 않는다.
  · 출력은 --out 아래에만 쓴다.

왜 필요한가: 2026-10-18 수정 전에는 `schema.OVRS_CCNL` 이 RSYM 을 빠뜨려 폭이 25 였다.
한 프레임에 레코드가 여러 개면 레코드마다 1칸씩 밀려, mu/ticks.csv 단계에서 이미
필드가 유실됐다. 그래서 **ticks.csv 로는 복구할 수 없고 raw frame 이 있어야 한다.**

raw 위치 (ws_shadow_logger/storage.py):
  raw/YYYY-MM-DD/frames_HH.txt      장중 무압축 append
  raw/YYYY-MM-DD/frames_HH.txt.gz   15:30 maintenance 가 gzip 으로 바꾼 것
  각 줄: "<수신 ISO ms>\\t<frame>"

사용:
  python -m research_20261018_rt_quality.scripts.recover_mu \\
      --raw-root <raw 가 들어있는 폴더> --day 2026-10-16 --out <출력폴더>
  (--raw-root 는 raw/ 의 **부모**. 예: Render 디스크의 $KIS_LOGGER_DATA_DIR 사본)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from ws_shadow_logger import mu  # noqa: E402
from ws_shadow_logger.parser import FrameError, parse_frame  # noqa: E402
from ws_shadow_logger.schema import OVRS_CCNL  # noqa: E402


def iter_raw(raw_root: Path, day: str):
    """raw/<day>/frames_HH.txt(.gz) 를 시각순으로 읽어 (recv, frame) 을 흘린다. 읽기 전용."""
    d = raw_root / "raw" / day
    if not d.is_dir():
        return
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if not p.name.startswith("frames_"):
            continue
        op = (gzip.open if p.name.endswith(".gz") else open)
        with op(p, "rt", encoding="utf-8", errors="replace") as fh:   # 읽기 전용
            for line in fh:
                if "\t" not in line:
                    continue
                ts, frame = line.rstrip("\n").split("\t", 1)
                try:
                    recv = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                yield recv, frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", required=True, help="raw/ 의 부모 폴더")
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    raw_root, out = Path(a.raw_root), Path(a.out)
    assert OVRS_CCNL[0] == "RSYM" and len(OVRS_CCNL) == 26, \
        "schema 가 아직 옛 25필드다 — 수정(f0cb468) 이후 코드에서 실행할 것"

    src = raw_root / "raw" / a.day
    if not src.is_dir():
        print(f"raw 없음: {src}")
        print("Render 디스크에서 그 폴더를 통째로 복사해 와야 한다 (원본은 그대로 둘 것).")
        return 2

    out.mkdir(parents=True, exist_ok=True)
    tp, bp = out / f"ticks_{a.day}.csv", out / f"bars_1m_{a.day}.csv"
    n_frames = n_mu = n_rec = n_tick = 0
    bad = 0
    rows = []
    with open(tp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=mu.TICK_COLUMNS)
        w.writeheader()
        for recv, frame in iter_raw(raw_root, a.day):
            n_frames += 1
            try:
                m = parse_frame(frame)
            except FrameError:
                bad += 1
                continue
            if m.get("type") != "data" or m.get("tr_id") != "HDFSCNT0":
                continue
            n_mu += 1
            for rec in m["records"]:
                n_rec += 1
                kt = mu.kst_ts(rec)
                px = rec.get("LAST")
                if kt is None or px in (None, ""):
                    continue
                feed = str(rec.get("RSYM") or "").strip() or "?"
                row = {"recv_kst": recv.isoformat(timespec="milliseconds"), "feed": feed,
                       "kst_ts": kt.isoformat(timespec="seconds"), "local_ymd": rec.get("XYMD"),
                       "local_hms": rec.get("XHMS"), "last": px, "evol": rec.get("EVOL"),
                       "tvol": rec.get("TVOL"), "pbid": rec.get("PBID"), "pask": rec.get("PASK"),
                       "vbid": rec.get("VBID"), "vask": rec.get("VASK"), "strn": rec.get("STRN"),
                       "mtyp": rec.get("MTYP"), "lag_s": round((recv - kt).total_seconds(), 3)}
                w.writerow(row)
                rows.append(row)
                n_tick += 1

    bars = mu.bars_from_ticks(rows) if hasattr(mu, "bars_from_ticks") else mu.build_bars(rows)
    with open(bp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=mu.BAR_COLUMNS)
        w.writeheader()
        for b in bars:
            w.writerow({k: b.get(k) for k in mu.BAR_COLUMNS})

    feeds = {}
    for r in rows:
        feeds[r["feed"]] = feeds.get(r["feed"], 0) + 1
    mk = [r for r in rows if "09:00" <= r["kst_ts"][11:16] <= "15:30"]
    print(f"frame {n_frames:,} (HDFSCNT0 {n_mu:,}, 파싱실패 {bad}) -> 레코드 {n_rec:,} -> tick {n_tick:,}")
    print(f"feed 별: {feeds}")
    print(f"한국 장중(09:00~15:30 KST) tick {len(mk):,}건")
    print(f"분봉 {len(bars):,}개  -> {bp.name}")
    if bars:
        print(f"  첫 분 {bars[0]['minute_kst']}  마지막 분 {bars[-1]['minute_kst']}")
    print(f"-> {tp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
