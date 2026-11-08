"""A(현행) vs B(신안) 재생 비교 — 거래 단위로 묶고 슬롯 연쇄효과까지 분해한다.

거래 = BUY 체결 1건과 그 다음 BUY 전까지의 SELL net_pnl 합.
일손익 = 그날 SELL net_pnl 합(미청산은 재생이 장마감 강제청산하므로 남지 않는다).
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]


def load(tag, d):
    p = OUT / f"{tag}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if p.exists() else None


def _day_exec(j):
    """당일 체결만 (side, sym, qty, px, net_pnl, exit_reason, 재생시각).

    `ex`(체결원장)에는 seed 용으로 함께 재생된 **직전일 체결까지** 들어 있다.
    `orders` 는 당일분만이고 재생시각 `t` 를 갖는다 — 둘을 순서대로 정합해 당일분만 뽑는다.
    """
    ex = list(j.get("ex") or [])
    out, pos = [], 0
    for o in j.get("orders") or []:
        key = (o["side"], o["sym"], float(o["qty"]), float(o["px"]))
        while pos < len(ex):
            e = ex[pos]
            pos += 1
            if (e.get("side"), e.get("symbol"), float(e.get("executed_qty") or 0),
                    float(e.get("executed_price") or 0)) == key:
                out.append(dict(t=o["t"], side=o["side"], sym=o["sym"],
                                qty=key[2], px=key[3],
                                pnl=float(e.get("net_pnl") or 0),
                                exit=e.get("exit_reason") or "",
                                acts=o.get("acts") or []))
                break
    return out


def trades(j):
    """당일 거래 = BUY 체결 1건과 그 다음 BUY 전까지의 SELL net_pnl 합."""
    out, cur = [], None
    for e in _day_exec(j):
        if e["side"] == "BUY":
            if cur:
                out.append(cur)
            sid = ""
            for a in e["acts"]:
                sid = a
            cur = dict(sid=sid, t=e["t"], sym=e["sym"], qty=e["qty"], px=e["px"],
                       pnl=0.0, exits=[])
        elif e["side"] == "SELL" and cur is not None:
            cur["pnl"] += e["pnl"]
            cur["exits"].append(e["exit"])
    if cur:
        out.append(cur)
    return out

def order_times(j):
    """진입 주문의 (시각, 심볼) — ex 의 timestamp 는 벽시계라 orders 를 쓴다."""
    return [(o["t"], o["sym"], o.get("acts")) for o in (j.get("orders") or []) if o["side"] == "BUY"]


A, B = {}, {}
missing = []
for d in DAYS:
    a, b = load("CG_A", d), load("CG_B", d)
    if a is None or b is None:
        missing.append(d)
        continue
    A[d], B[d] = a, b
if missing:
    print(f"!! 재생 결과 없음: {missing}")
    if not A:
        sys.exit(1)
DAYS = [d for d in DAYS if d in A]


def metrics(store):
    tr, daily = [], {}
    for d in DAYS:
        t = trades(store[d])
        tr.extend((d, x) for x in t)
        daily[d] = sum(x["pnl"] for x in t)
    pnls = [x["pnl"] for _, x in tr]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p < 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for d in DAYS:
        cum += daily[d]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    srt = sorted(pnls, reverse=True)
    tot = sum(pnls)
    return dict(
        n=len(tr), total=tot, pf=(sum(wins) / abs(sum(loss))) if loss else float("inf"),
        mdd=mdd, worst_day=min(daily.values()) if daily else 0.0,
        worst_day_at=min(daily, key=daily.get) if daily else "",
        worst_trade=min(pnls) if pnls else 0.0,
        win=len(wins) / len(pnls) * 100 if pnls else 0.0,
        loss_days=sum(1 for v in daily.values() if v < 0),
        avg=tot / len(pnls) if pnls else 0.0,
        ex1=tot - (srt[0] if srt else 0), ex2=tot - sum(srt[:2]),
        daily=daily, trades=tr)


ma, mb = metrics(A), metrics(B)
print(f"재생 {len(DAYS)}일 ({DAYS[0]}~{DAYS[-1]}) · 운영구성 CHOP·MU ON·XS ON · HEAD d0e7bef")
print()
print(f"{'지표':16s} {'A 현행':>14s} {'B 신안':>14s} {'차이':>14s}")
rows = [("거래수", "n", 0), ("총손익", "total", 0), ("PF", "pf", 3), ("MDD", "mdd", 0),
        ("최악일", "worst_day", 0), ("최악거래", "worst_trade", 0), ("승률 %", "win", 1),
        ("손실일수", "loss_days", 0), ("거래당 평균", "avg", 0),
        ("상위1 제외", "ex1", 0), ("상위2 제외", "ex2", 0)]
for lab, k, prec in rows:
    va, vb = ma[k], mb[k]
    f = (lambda v: f"{v:,.0f}") if prec == 0 else (lambda v: f"{v:,.{prec}f}")
    print(f"{lab:16s} {f(va):>14s} {f(vb):>14s} {f(vb-va):>14s}")
print(f"{'최악일 날짜':16s} {ma['worst_day_at']:>14s} {mb['worst_day_at']:>14s}")

print()
print("=== 일자별 손익 ===")
print(f"{'날짜':10s} {'A 건수':>6s} {'A 손익':>12s} {'B 건수':>6s} {'B 손익':>12s} {'차이':>12s}")
for d in DAYS:
    na = sum(1 for dd, _ in ma["trades"] if dd == d)
    nb = sum(1 for dd, _ in mb["trades"] if dd == d)
    diff = mb["daily"][d] - ma["daily"][d]
    mark = "  <<" if abs(diff) > 1 else ""
    print(f"{d:10s} {na:6d} {ma['daily'][d]:12,.0f} {nb:6d} {mb['daily'][d]:12,.0f} {diff:12,.0f}{mark}")

print()
print("=== ① 새 게이트가 직접 차단한 거래 전수 ===")
blocked = []
for d in DAYS:
    for e in B[d].get("cg") or []:
        # 하네스는 seed 용으로 직전일도 재생한다 — 그날 것만 센다
        if e.get("blocked") and e["t"][:10].replace("-", "") == d:
            blocked.append((d, e))
_judged = sum(1 for d in DAYS for e in (B[d].get("cg") or []) if e["t"][:10].replace("-", "") == d)
print(f"  차단 {len(blocked)}건 / 게이트 판정 {_judged}건 (당일분만)")
print(f"  {'날짜':10s} {'시각':6s} {'MU':14s} {'방향':10s} {'ER60':>8s} {'gap':>8s} {'A 실제손익':>12s}")
a_by_time = {(d, x["t"][11:16]): x for d in DAYS for x in trades(A[d])}


def a_hit(d, e):
    """차단 시각에 A 가 실제로 진입했던 거래(= 그 후보가 먹었을 슬롯)."""
    return a_by_time.get((d, e["t"][11:16]))


for d, e in blocked:
    hit = a_hit(d, e)
    pnl = f"{hit['pnl']:,.0f}" if hit else "A도 미진입"
    print(f"  {d:10s} {e['t'][11:16]:6s} {e['mu']:14s} {e['dir']:10s} "
          f"{(e['er60'] if e['er60'] is not None else float('nan')):8.4f} "
          f"{(e['gap'] if e['gap'] is not None else float('nan')):8.3f} {pnl:>12s}")

print()
print("=== ② 차단으로 새로 들어간 후속 거래 전수 ===")
for d in DAYS:
    ta, tb = trades(A[d]), trades(B[d])
    sa = {x["t"][11:16] for x in ta}
    sb = {x["t"][11:16] for x in tb}
    new = [x for x in tb if x["t"][11:16] not in sa]
    gone = [x for x in ta if x["t"][11:16] not in sb]
    if not new and not gone:
        continue
    print(f"  [{d}]")
    for x in gone:
        print(f"    - 사라짐  {x['t'][11:16]} {x['sid'][:34]:34s} {x['sym']} 손익 {x['pnl']:>12,.0f} [{'|'.join(x['exits'])}]")
    for x in new:
        print(f"    + 새로진입 {x['t'][11:16]} {x['sid'][:34]:34s} {x['sym']} 손익 {x['pnl']:>12,.0f} [{'|'.join(x['exits'])}]")

print()
print("=== ③ 효과 분해 ===")
direct = 0.0
for d, e in blocked:
    hit = a_hit(d, e)
    if hit:
        direct += -hit["pnl"]
total = mb["total"] - ma["total"]
print(f"  직접 차단 효과 (A에서 그 거래 손익의 반대)   {direct:+15,.0f}")
print(f"  슬롯 연쇄효과 (나머지)                      {total-direct:+15,.0f}")
print(f"  최종 총효과 (B − A)                        {total:+15,.0f}")

print()
print("=== ④ MU 상태별 차단 ===")
for mu in ("ENTRY_CONFIRM", "ENTRY_WAIT"):
    sub = [(d, e) for d, e in blocked if e["mu"] == mu]
    eff = sum(-(a_hit(d, e)["pnl"]) for d, e in sub if a_hit(d, e))
    entered = sum(1 for d, e in sub if a_hit(d, e))
    print(f"  {mu:14s} 차단 {len(sub):2d}건 (그 중 A 에서 실제 진입했던 것 {entered}건)  직접효과 {eff:+,.0f}")

print()
print("=== ⑤ 2026-11-06 ===")
if "20261106" in A:
    for e in [x for x in (B["20261106"].get("cg") or []) if x["t"][:10] == "2026-11-06"]:
        print(f"  {e['t'][11:16]} {e['mu']:14s} {e['dir']:10s} ER60 "
              f"{(e['er60'] if e['er60'] is not None else float('nan')):.4f} gap "
              f"{(e['gap'] if e['gap'] is not None else float('nan')):+.3f} -> "
              f"{'차단' if e['blocked'] else '통과'}")
    print(f"  A 거래: " + ", ".join(f"{x['sid'][:28]}={x['pnl']:,.0f}" for x in trades(A["20261106"])))
    print(f"  B 거래: " + ", ".join(f"{x['sid'][:28]}={x['pnl']:,.0f}" for x in trades(B["20261106"])))
