"""H6: 부품 A 집중도 + 안전성 지표. READ-ONLY."""
import glob, json, os, sys
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"; DATA = ROOT + "/wk/data"; TD = REPO + "/data/cache"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__)); KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns); trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
_c = {}
def one(day, tag):
    k=(day,tag)
    if k not in _c:
        r=None
        for sd in (DATA,TD):
            f=f"{sd}/replay_{day}_{tag}_1m.csv"
            if os.path.exists(f):
                df=pd.read_csv(f); dt=pd.to_datetime(df["datetime"])
                df["datetime"]=dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
                r=df.sort_values("datetime").reset_index(drop=True); break
        _c[k]=r
    return _c[k]
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
rows=[]
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3+"/REAL_A_*.json")}):
    g=L[L.day==d].sort_values("mins")
    for t in trades(json.load(open(ap(d),encoding="utf-8"))):
        if t.get("regime")!="CHOP": continue
        sym=LONG if t["dir"].startswith("UP") else "0197X0"
        e=one(d,"long" if sym==LONG else "inverse")
        if e is None: continue
        ts=pd.DatetimeIndex(e["datetime"]); i0=int(ts.searchsorted(t["entry"])); px0=t["px_in"]
        m=t["entry"].hour*60+t["entry"].minute
        want="UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx=g[(g.mins>m)&(g["dir"]==want)]
        opp=int(nx.iloc[0]["mins"]) if len(nx) else 15*60+20
        iO=int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}",tz=KST)))
        iE=int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20",tz=KST)))
        lim=min(iO,iE,len(e))
        n=lambda i,w:(float(e[w].iloc[i])-px0)/px0*100-DRAG
        j1=None
        for i in range(i0,max(lim,i0+1)):
            if n(i,"low")<=-1.0: break
            if n(i,"high")>=1.0: j1=i; break
        if j1 is None: continue
        r=dict(day=d,at=t["entry"].strftime("%H:%M"),t1=(ts[j1]-t["entry"]).total_seconds()/60,
               notional=t["q"]*px0,rs="+".join(dict.fromkeys(t["reasons"])))
        for F in (0.1,0.2):
            out=None
            for i in range(j1,max(lim,j1+1)):
                if n(i,"low")<=F: out=F; break
            if out is None: out=n(max(lim-1,j1),"close")
            r[f"d{F}"]=((0.2+0.8*out)-1.0)/100*r["notional"]
        rows.append(r)
T=pd.DataFrame(rows)
for F in (0.1,0.2):
    c=f"d{F}"; s=T.sort_values(c,ascending=False)
    print(f"\n## 잔량 고정스탑 +{F}% — CHOP {len(T)}건")
    print(f"   합 {T[c].sum():+,.0f} · {int((T[c]>0).sum())}승 {int((T[c]<0).sum())}패 · 1건 최악 {T[c].min():+,.0f}")
    for k in (1,2,3):
        print(f"   상위 {k}건 제외 -> {T[c].sum()-s[c].head(k).sum():+,.0f}")
print("\n## 건별 (+0.2%)")
print("| 일자 | 진입 | +1% 도달 | 차이(원) | 실제사유 |")
print("|---|---|---|---|---|")
for _,r in T.sort_values("d0.2",ascending=False).iterrows():
    print(f"| {r.day} | {r['at']} | {r.t1:.0f}분 | **{r['d0.2']:+,.0f}** | {r.rs[:30]} |")
