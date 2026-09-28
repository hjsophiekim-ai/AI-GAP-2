import pickle,sys,bisect,pandas as pd,numpy as np
from collections import defaultdict,Counter
sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0,"proj"); sys.path.insert(0,"proj/scripts")
N=sys.argv[1] if len(sys.argv)>1 else "R0"
L=lambda n:pickle.load(open(f"out_{n}.pkl","rb"))["trades"]
D=list(pickle.load(open("_ctx80.pkl","rb"))["dates"])
B,R0=L("BASE"),L(N)
pnl=lambda t:t["net_pct"]*t["w1a"]
rows=sorted(((pd.Timestamp(t["exit_time"]),t["h50_held"],t["tp1_hit"]) for t in B),key=lambda r:r[0]); ex=[r[0] for r in rows]
def rg(ts):
    k=bisect.bisect_left(ex,pd.Timestamp(ts))
    if k<10: return "WARMUP"
    r=rows[k-10:k]; return "CHOP" if sum(z[1] for z in r)>=4 and sum(z[2] for z in r)<=2 else "TREND"
byd=defaultdict(list)
for t in R0: byd[t["date"]].append(t)
out=[]
for d in D:
    ts=sorted(byd[d],key=lambda t:t["entry_time"])
    if not ts: continue
    f=1.0
    for t in sorted(ts,key=lambda t:t["exit_time"]): f*=1+pnl(t)/100
    out.append(dict(d=d,ret=(f-1)*100,n=len(ts),reg=rg(ts[0]["entry_time"]),first=pnl(ts[0]),
       maxmfe=max(t["peak_net_pct"] for t in ts),
       seq=" ".join(f"{pd.Timestamp(t['entry_time']).strftime('%H:%M')}{t['direction'][0]}{t['net_pct']:+.2f}({t['exit_reason'][:12]})" for t in ts)))
df=pd.DataFrame(out)
loss=df[df.ret<0]
print(N,"손실일",len(loss),"| regime:",dict(Counter(loss.reg)),"| 손실일 합",round(loss.ret.sum(),2))
print("저수익(0~2%)일",((df.ret>=0)&(df.ret<2)).sum(), dict(Counter(df[(df.ret>=0)&(df.ret<2)].reg)))
print("≥2%일",(df.ret>=2).sum(), dict(Counter(df[df.ret>=2].reg)))
print("손실일 중 첫 거래 손실 %d/%d | 한때 MFE>=1.5%% %d/%d | 거래수 %s"%((loss["first"]<0).sum(),len(loss),(loss.maxmfe>=1.5).sum(),len(loss),dict(Counter(loss.n))))
print("\n손실일 전량:")
for r in loss.sort_values("d").itertuples():
    print(f"  {r.d} {r.reg:6s} 일 {r.ret:+6.2f}% | 최대MFE {r.maxmfe:4.2f} | {r.seq}")
print("\nregime별(첫 진입) 일수익:"); print(df.groupby("reg").ret.agg(n="count",평균="mean",중앙="median",ge2=lambda s:(s>=2).mean()*100,손실=lambda s:(s<0).sum()).round(2).to_string())
