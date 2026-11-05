"""A/B/C (09시대 MU 예정공백) 13일 비교 + 실데이터 참고."""
import json, sys
from pathlib import Path
sys.path.insert(0, r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad/wt_study")
sys.stdout.reconfigure(encoding="utf-8")
from compare import episodes  # noqa: E402

O = Path(__file__).parent / "out"
FX = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad/out_fx")
DAYS = "20261016 20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030 20261102 20261103".split()
SRC = {"실데이터": lambda d: FX / f"FX_{d}.json", "A": lambda d: O / f"GA_{d}.json",
       "B": lambda d: O / f"GB_{d}.json", "C": lambda d: O / f"GC_{d}.json"}


def load(tag):
    tr = []
    for d in DAYS:
        p = SRC[tag](d)
        if not p.exists():
            return None
        for e in episodes(json.loads(p.read_text(encoding="utf-8")), d):
            tr.append((d, e["t"][:5], e["sym"], e["pnl"], e["exits"][-1][3] if e["exits"] else ""))
    return tr


def stats(tr):
    pnl = [x[3] for x in tr]
    win = [p for p in pnl if p > 0]; loss = [p for p in pnl if p <= 0]
    daily = {}
    for x in tr:
        daily[x[0]] = daily.get(x[0], 0) + x[3]
    eq = peak = mdd = 0
    for d in DAYS:
        eq += daily.get(d, 0); peak = max(peak, eq); mdd = max(mdd, peak - eq)
    s = sorted(pnl, reverse=True)
    return dict(n=len(pnl), total=sum(pnl), pf=(sum(win) / -sum(loss)) if loss and sum(loss) < 0 else float("inf"),
                mdd=mdd, worst=min(daily.values()) if daily else 0,
                worst_day=min(daily, key=daily.get) if daily else "", wr=len(win) / len(pnl) * 100 if pnl else 0,
                ex1=sum(s[1:]), ex2=sum(s[2:]))


res = {t: load(t) for t in SRC}
print(f"{'':8}{'거래':>5}{'총손익':>13}{'PF':>7}{'MDD':>11}{'최악일':>12}{'승률':>7}{'상위1제외':>13}{'상위2제외':>13}")
for t, tr in res.items():
    if tr is None:
        print(f"{t:8} (미완료)"); continue
    s = stats(tr)
    print(f"{t:8}{s['n']:>5}{s['total']:>+13,}{s['pf']:>7.2f}{s['mdd']:>11,}{s['worst']:>+12,}({s['worst_day'][4:]}){s['wr']:>6.1f}%"
          f"{s['ex1']:>+13,}{s['ex2']:>+13,}")
for t, tr in res.items():
    if tr is None:
        continue
    nine = [x for x in tr if "09:00" <= x[1] < "10:00"]
    print(f"\n[{t}] 9시대 {len(nine)}건 합계 {sum(x[3] for x in nine):+,}")
    for x in nine:
        print(f"   {x[0][4:6]}/{x[0][6:]} {x[1]} {x[2]} {x[3]:+,} ({x[4]})")
base = set((x[0], x[1], x[2]) for x in res["A"] or [])
for t in ("B", "C"):
    tr = res[t]
    if tr is None:
        continue
    cur = {(x[0], x[1], x[2]): x for x in tr}
    add = [cur[k] for k in cur if k not in base]
    rem = [x for x in res["A"] if (x[0], x[1], x[2]) not in cur]
    print(f"\n[{t} vs A] 추가 {len(add)}건 {sum(x[3] for x in add):+,} / 사라짐 {len(rem)}건 {sum(x[3] for x in rem):+,}")
    for x in add:
        print(f"   + {x[0][4:]} {x[1]} {x[2]} {x[3]:+,} ({x[4]})")
    for x in rem:
        print(f"   - {x[0][4:]} {x[1]} {x[2]} {x[3]:+,} ({x[4]})")
