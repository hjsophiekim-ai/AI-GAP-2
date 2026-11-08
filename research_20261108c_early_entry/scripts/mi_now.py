"""즉시진입(플래그봉 완성 시점) vs T+3 진입 — 같은 플래그, 진입 시점만 다르게 (READ ONLY).

플래그봉 라벨 시각 F 에 대해
  즉시 = F+3분 (그 3분봉이 완성돼 플래그가 인지되는 시각)
  T+3  = F+6분 (production 이 실제로 체결하는 시각)
둘 다 '그 시각에 끝난 1분봉 종가' 로 진입. 미래 데이터 없음.
괴리 확대 = mu_hy_gap(F+3) > mu_hy_gap(F)  — 새 임계값 없이 부호만.
"""
import csv, sys
from datetime import datetime, timedelta
from pathlib import Path
import cx_lib as X  # noqa
import pandas as pd
import wx_lib as W
from app.trading.macd2 import config, mu_catchup
from app.trading.macd2.models import Direction
sys.stdout.reconfigure(encoding="utf-8")
REPO=Path(r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2")
NOV=REPO/"research_20261020_h50_hold"/"data"
OCT=Path(r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp/data")
def bars(day,kind):
    ymd=day.replace("-","")
    for base in (OCT,NOV):      # 11/06 은 OCT 에 보정본이 복사돼 있다
        p=base/f"replay_{ymd}_{kind}_1m.csv"
        if p.exists():
            df=pd.read_csv(p); df["datetime"]=pd.to_datetime(df["datetime"]).dt.tz_localize(config.KST)
            return df.sort_values("datetime").reset_index(drop=True)
    return None
def fwd(etf, t, horizon=(15,30)):
    if etf is None: return {}
    done=etf[etf["datetime"]+pd.Timedelta(minutes=1)<=t]
    if done.empty: return {}
    px=float(done["close"].iloc[-1]); fut=etf[etf["datetime"]>=t]
    o={"px":px}
    for h in horizon:
        seg=etf[etf["datetime"]+pd.Timedelta(minutes=1)<=t+timedelta(minutes=h)]
        o[f"r{h}"]=round((float(seg["close"].iloc[-1])/px-1)*100,3) if not seg.empty else None
    if fut.empty: return o
    o["mfe"]=round((float(fut["high"].max())/px-1)*100,3)
    o["mae"]=round((float(fut["low"].min())/px-1)*100,3)
    up,dn=px*1.015,px*0.985; first="NONE"
    for _,row in fut.iterrows():
        hi,lo=float(row["high"]),float(row["low"])
        if hi>=up and lo<=dn: first="BOTH"; break
        if hi>=up: first="UP"; break
        if lo<=dn: first="DOWN"; break
    o["touch"]=first
    return o
SRC=list(csv.DictReader(open("out_tag2.csv",encoding="utf-8-sig")))
rows=[]
for day in sorted({r["day"] for r in SRC}):
    hy=bars(day,"hynix"); mus=W.mu_series(day); etfs={}
    if hy is None or mus is None: continue
    for r in [x for x in SRC if x["day"]==day]:
        d=Direction(r["dir"]); F=datetime.strptime(f"{day} {r['flag']}","%Y-%m-%d %H:%M").replace(tzinfo=config.KST)
        now_=F+timedelta(minutes=3); t3=F+timedelta(minutes=6)
        k="long" if d==Direction.UP_RED else "inverse"
        if k not in etfs: etfs[k]=bars(day,k)
        a=fwd(etfs[k],now_); b=fwd(etfs[k],t3)
        if not a or not b: continue
        g0=mu_catchup.evaluate(d,F,F,hy,mu_series=mus,cttr={"cttr":None,"state":"UNAVAILABLE"}).get("mu_hy_gap")
        g1=mu_catchup.evaluate(d,F,now_,hy,mu_series=mus,cttr={"cttr":None,"state":"UNAVAILABLE"}).get("mu_hy_gap")
        rows.append(dict(day=day,flag=r["flag"],dir=r["dir"],mu_status=r["mu_status"],
                         lead=r["lead"],tgap=r["tgap"],thold=r["thold"],
                         g0=g0,g1=g1,widen=(g0 is not None and g1 is not None and g1>g0),
                         n_px=a["px"],n_r15=a.get("r15"),n_r30=a.get("r30"),n_mfe=a.get("mfe"),
                         n_mae=a.get("mae"),n_touch=a.get("touch"),
                         t_px=b["px"],t_r15=b.get("r15"),t_r30=b.get("r30"),t_mfe=b.get("mfe"),
                         t_mae=b.get("mae"),t_touch=b.get("touch")))
    del hy,mus,etfs
    print("done",day,file=sys.stderr)
with open("out_now.csv","w",newline="",encoding="utf-8-sig") as fo:
    w=csv.DictWriter(fo,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print(f"out_now.csv — {len(rows)}건",file=sys.stderr)
