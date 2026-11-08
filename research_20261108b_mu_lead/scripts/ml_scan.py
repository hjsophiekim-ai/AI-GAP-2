"""MU 선행 플래그 — 비왕복장 × T+3 유지·괴리확대 × 큰 MU-HY 괴리 (READ ONLY, 재생 없음).

MU 플래그 정의는 2026-11-04 연구(m1_flags.py)와 동일:
  MU 1분봉 종가 -> mu_assist._macd_hist -> production 제로크로스 + repeat suppression
  인지 시각 F_end = 그 1분봉 종료 시각. 판정 T3 = F_end + 3분 (사용자 정의).

조건 (전부 기존 값·기존 함수, 새 임계값은 ER 0.1223 하나뿐 — 2026-11-07 고정값 재사용):
  ① 비왕복장    : 하이닉스 최근 60분 효율비 >= 0.1223
  ② T+3 유지·확대: MU hist 가 T3 에도 같은 방향 ∧ |hist(T3)| > |hist(F_end)|
  ③ 큰 MU-HY 괴리: mu_catchup.evaluate 의 mu_hy_gap (방향부호) — 값만 기록, 분위수로 본다
  ④ 선행성      : 같은 방향 하이닉스 확정 플래그가 아직 없음 (있으면 몇 분 뒤인지 기록)

결과: 방향 ETF 1분봉. 진입가 = T3 에 끝난 1분봉 종가. 30분 전방 + MFE/MAE + ±1.5% 선터치.
"""
import csv, os, sys
from datetime import datetime, timedelta
from pathlib import Path
import cx_lib as X  # noqa
import pandas as pd
import wx_lib as W
from app.trading.macd2 import config, mu_assist, mu_catchup, signal_engine
from app.trading.macd2.models import Direction
sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2")
NOV = REPO/"research_20261020_h50_hold"/"data"; FIX = REPO/"research_20261108_chop_gate"/"bars_fix"
OCT = Path(r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp/data")
DAYS = ["2026-10-19","2026-10-20","2026-10-21","2026-10-22","2026-10-23","2026-10-26",
        "2026-10-27","2026-10-28","2026-10-29","2026-10-30","2026-11-02","2026-11-03",
        "2026-11-04","2026-11-05","2026-11-06"]
ERCUT = 0.1223

def bars(day, kind):
    ymd = day.replace("-","")
    for base in (FIX, NOV, OCT):
        p = base/f"replay_{ymd}_{kind}_1m.csv"
        if p.exists():
            df = pd.read_csv(p); df["datetime"]=pd.to_datetime(df["datetime"]).dt.tz_localize(config.KST)
            return df.sort_values("datetime").reset_index(drop=True)
    return None

def mu_flags(series):
    hist = mu_assist._macd_hist(series); out=[]; last=None
    for i in range(1, len(hist)):
        if i + 1 < int(config.EMA_SLOW): continue
        prev=round(float(hist.iloc[i-1]),6); cur=round(float(hist.iloc[i]),6)
        if prev<=0 and cur>0: pat=Direction.UP_RED
        elif prev>=0 and cur<0: pat=Direction.DOWN_BLUE
        else: continue
        if last==pat: continue
        last=pat
        out.append((hist.index[i].to_pydatetime()+timedelta(minutes=1), pat, cur))
    return out, hist

def hy_flags(df, day):
    b3 = signal_engine.resample_completed_3m(df, pd.Timestamp(f"{day} 15:40", tz=config.KST))
    out=[]; prev=None
    for i in range(config.EMA_SLOW+2, len(b3)+1):
        s = signal_engine.calculate_macd(b3.iloc[:i])
        if s is None: continue
        d = signal_engine.evaluate_macd_crossover(s, prev)
        if d != Direction.HOLD:
            prev=d; out.append((s.bar_dt.astimezone(config.KST), d))
    return out

def er60(hy, now):
    n = now.replace(tzinfo=None) if now.tzinfo else now
    ts = hy["datetime"].dt.tz_convert(config.KST).dt.tz_localize(None)
    m = ((ts+pd.Timedelta(minutes=1))<=n) & (ts >= n-pd.Timedelta(minutes=60))
    c = pd.to_numeric(hy.loc[m.values,"close"],errors="coerce").dropna()
    if len(c)<5: return None
    tot=float(c.diff().abs().sum())
    return None if tot<=0 else abs(float(c.iloc[-1])-float(c.iloc[0]))/tot

COLS=["day","f_end","t3","dir","er60","chop","mu_h0","mu_h3","hold","expand",
      "mu_sess","hy_sess","gap","gap3","gap_widen","hy_lead_min","entry_px","r15","r30","mfe","mae","touch"]
rows=[]
for day in DAYS:
    hy = bars(day,"hynix"); mus = W.mu_series(day)
    if hy is None or mus is None: print(day,"데이터 없음",file=sys.stderr); continue
    flags,hist = mu_flags(mus)
    hyf = hy_flags(hy, day)
    etfs={}
    for fend, d, h0 in flags:
        fend = fend.replace(tzinfo=config.KST) if fend.tzinfo is None else fend
        if not (datetime.strptime(f"{day} 09:00","%Y-%m-%d %H:%M").replace(tzinfo=config.KST)
                <= fend <= datetime.strptime(f"{day} 15:20","%Y-%m-%d %H:%M").replace(tzinfo=config.KST)):
            continue
        t3 = fend + timedelta(minutes=3)
        hn = hist[hist.index <= t3.replace(tzinfo=None)]
        h3 = float(hn.iloc[-1]) if len(hn) else None
        s = 1.0 if d==Direction.UP_RED else -1.0
        hold = h3 is not None and (s*h3 > 0)
        expand = hold and abs(h3) > abs(h0)
        e = er60(hy, t3)
        cu  = mu_catchup.evaluate(d, fend, t3, hy, mu_series=mus, cttr={"cttr":None,"state":"UNAVAILABLE"})
        cu3 = mu_catchup.evaluate(d, fend, t3-timedelta(minutes=3), hy, mu_series=mus, cttr={"cttr":None,"state":"UNAVAILABLE"})
        gap, gapp = cu.get("mu_hy_gap"), cu3.get("mu_hy_gap")
        lead = next((int((t-fend).total_seconds()//60) for t,dd in hyf if dd==d and t>=fend), None)
        k = "long" if d==Direction.UP_RED else "inverse"
        if k not in etfs: etfs[k]=bars(day,k)
        f = W.forward(etfs[k], t3, horizon_min=(15,30)) if etfs[k] is not None else {}
        mfe=mae=None
        if f:
            fut=etfs[k][etfs[k]["datetime"]>=t3]; px=f["entry_px"]
            mfe=round((float(fut["high"].max())/px-1)*100,3); mae=round((float(fut["low"].min())/px-1)*100,3)
        rows.append(dict(day=day,f_end=f"{fend:%H:%M}",t3=f"{t3:%H:%M}",dir=d.value,
                         er60=None if e is None else round(e,4), chop=None if e is None else (e<ERCUT),
                         mu_h0=round(h0,4), mu_h3=None if h3 is None else round(h3,4),
                         hold=hold, expand=expand,
                         mu_sess=cu.get("mu_session_pct"), hy_sess=cu.get("hy_session_pct"),
                         gap=gap, gap3=gapp,
                         gap_widen=(gap is not None and gapp is not None and gap>gapp),
                         hy_lead_min=lead, entry_px=f.get("entry_px"), r15=f.get("r15"),
                         r30=f.get("r30"), mfe=mfe, mae=mae, touch=f.get("touch")))
    del hy,mus,etfs
    print("done",day,file=sys.stderr)
with open("out_mulead.csv","w",newline="",encoding="utf-8-sig") as fo:
    w=csv.DictWriter(fo,fieldnames=COLS); w.writeheader()
    for r in rows: w.writerow({c:r.get(c,"") for c in COLS})
print(f"out_mulead.csv — {len(rows)}건",file=sys.stderr)
