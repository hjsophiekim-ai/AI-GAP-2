"""로거 1초 스냅샷 -> 재생용 1분봉 (replay_<day>_{hynix,long,inverse}_1m.csv, KIS 분봉과 같은 형식).

price = 그 초의 최신 체결가, 거래량 = acml_vol 증가분. hynix 08:00~, ETF 09:00~ (KIS 형식과 동일).
python build_bars.py <snapshots/YYYY-MM-DD 폴더> <YYYYMMDD> <출력폴더>
"""
import csv, sys
from pathlib import Path

src, day, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
for sym, tag, start in (("000660", "hynix", "08:00"), ("0193T0", "long", "09:00"), ("0197X0", "inverse", "09:00")):
    bars, prev_vol = {}, None
    with open(src / f"{sym}.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ts = r["ts"][:19]
            if ts[11:16] < start or ts[11:16] >= "15:30":
                continue
            try:
                px = float(r["price"])
                vol = float(r["acml_vol"]) if r.get("acml_vol") else None
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            m = ts[:16].replace("T", " ") + ":00"
            dv = 0.0 if (vol is None or prev_vol is None) else max(0.0, vol - prev_vol)
            if vol is not None:
                prev_vol = vol
            b = bars.get(m)
            if b is None:
                bars[m] = [px, px, px, px, dv]
            else:
                b[1] = max(b[1], px); b[2] = min(b[2], px); b[3] = px; b[4] += dv
    p = out / f"replay_{day}_{tag}_1m.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for m in sorted(bars):
            o, h, l, c, v = bars[m]
            w.writerow([m, o, h, l, c, int(v)])
    print(tag, len(bars), "bars", min(bars), "~", max(bars))
