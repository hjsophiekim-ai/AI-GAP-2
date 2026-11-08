"""WAIT 선택진입 반대gap 조건 A/B 비교 — A=현행(cttr MATCH ∧ gap<=400) vs B=cttr MATCH 만.

B 는 config.MU_WAIT_OPPOSITE_GAP_MAX 만 덮은 재생이다(WK_WGAP). 그 외는 전부 동일.
거래는 당일 orders 와 ex 를 정합해 당일분만 센다(하네스는 seed 용으로 전일도 재생한다).
"""
import json, os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261019","20261020","20261021","20261022","20261023","20261026","20261027",
        "20261028","20261029","20261030","20261102","20261103","20261104","20261105","20261106"]

def trades(j):
    ex=list(j.get("ex") or []); out=[]; pos=0; cur=None
    for o in j.get("orders") or []:
        key=(o["side"],o["sym"],float(o["qty"]),float(o["px"]))
        while pos<len(ex):
            e=ex[pos]; pos+=1
            if (e.get("side"),e.get("symbol"),float(e.get("executed_qty") or 0),
                float(e.get("executed_price") or 0))==key:
                if o["side"]=="BUY":
                    if cur: out.append(cur)
                    cur=dict(t=o["t"][11:16],sym=o["sym"],qty=o["qty"],px=o["px"],pnl=0.0,ex=[])
                elif cur is not None:
                    cur["pnl"]+=float(e.get("net_pnl") or 0); cur["ex"].append(e.get("exit_reason") or "")
                break
    if cur: out.append(cur)
    return out

A,B={},{}
miss=[]
for d in DAYS:
    pa,pb = OUT/f"CG_A_{d}.json", OUT/f"WG_B_{d}.json"
    if not (pa.exists() and pb.exists()): miss.append(d); continue
    A[d]=trades(json.load(open(pa,encoding="utf-8"))); B[d]=trades(json.load(open(pb,encoding="utf-8")))
if miss: print(f"!! 결과 없음: {miss}")
D=[d for d in DAYS if d in A]

def met(S):
    tr=[x for d in D for x in S[d]]; pn=[x["pnl"] for x in tr]
    daily={d:sum(x["pnl"] for x in S[d]) for d in D}
    w=[p for p in pn if p>0]; l=[p for p in pn if p<0]
    cum=peak=mdd=0.0
    for d in D:
        cum+=daily[d]; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    s=sorted(pn,reverse=True); tot=sum(pn)
    return dict(n=len(tr),total=tot,pf=(sum(w)/abs(sum(l)) if l else float("inf")),mdd=mdd,
                worst_day=min(daily.values()) if daily else 0,worst=min(pn) if pn else 0,
                win=100*len(w)/len(pn) if pn else 0,ld=sum(1 for v in daily.values() if v<0),
                avg=tot/len(pn) if pn else 0,ex1=tot-(s[0] if s else 0),ex2=tot-sum(s[:2]),daily=daily)

ma,mb=met(A),met(B)
print(f"재생 {len(D)}일 ({D[0]}~{D[-1]}) · 운영구성 CHOP·MU ON·XS ON · HEAD d0e7bef")
print(f"A = 현행 (체결강도 MATCH ∧ 반대gap<=400)   B = 체결강도 MATCH 만")
print()
print(f"{'지표':14s} {'A 현행':>14s} {'B 신안':>14s} {'차이':>14s}")
for lab,k,p in (("거래수","n",0),("총손익","total",0),("PF","pf",3),("MDD","mdd",0),
                ("최악일","worst_day",0),("최악거래","worst",0),("승률 %","win",1),
                ("손실일수","ld",0),("거래당 평균","avg",0),("상위1 제외","ex1",0),("상위2 제외","ex2",0)):
    f=(lambda v:f"{v:,.0f}") if p==0 else (lambda v:f"{v:,.{p}f}")
    print(f"{lab:14s} {f(ma[k]):>14s} {f(mb[k]):>14s} {f(mb[k]-ma[k]):>14s}")
print()
print("=== 일자별 ===")
print(f"{'날짜':10s} {'A건':>4s} {'A손익':>12s} {'B건':>4s} {'B손익':>12s} {'차이':>12s}")
for d in D:
    diff=mb["daily"][d]-ma["daily"][d]
    print(f"{d:10s} {len(A[d]):4d} {ma['daily'][d]:12,.0f} {len(B[d]):4d} {mb['daily'][d]:12,.0f} {diff:12,.0f}"
          + ("  <<" if abs(diff)>1 else ""))
print()
print("=== 달라진 거래 전수 ===")
for d in D:
    sa={x["t"] for x in A[d]}; sb={x["t"] for x in B[d]}
    new=[x for x in B[d] if x["t"] not in sa]; gone=[x for x in A[d] if x["t"] not in sb]
    chg=[(x,y) for x in A[d] for y in B[d] if x["t"]==y["t"] and abs(x["pnl"]-y["pnl"])>1]
    if not (new or gone or chg): continue
    print(f"  [{d}]")
    for x in gone: print(f"    - 사라짐  {x['t']} {x['sym']} {x['pnl']:>12,.0f} [{'|'.join(x['ex'])}]")
    for x in new:  print(f"    + 새로진입 {x['t']} {x['sym']} qty={x['qty']} px={x['px']} {x['pnl']:>12,.0f} [{'|'.join(x['ex'])}]")
    for x,y in chg: print(f"    ~ 손익변경 {x['t']} {x['pnl']:,.0f} -> {y['pnl']:,.0f}")
