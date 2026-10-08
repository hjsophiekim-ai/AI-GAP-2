"""사전분석: 9~10월 B3_SL 손절 거래의 손절 후 5/10/15/20분 회복률. READ-ONLY.

회복 기준은 '그 포지션 ETF 가격이 진입가(본전)로 돌아왔는가'. 수수료/세금은 net 기준으로
production 원장과 맞추기 위해 손절 net% 와 같은 축(= krw/(q*px_in)*100)을 쓴다.
박스 판정은 손절 시점 하이닉스 1분봉 기준(30분 박스, 마지막 3분 제외) — 연구1과 같은 정의.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
SPD = W + "/sp/data"
sys.path.insert(0, W)
from feat import trades  # noqa
pd.set_option("display.width", 220)

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]
TAG = {"0193T0": "long", "0197X0": "inverse"}
_c = {}


def bars(day, tag):
    k = (day, tag)
    if k not in _c:
        p = f"{SPD}/replay_{day}_{tag}_1m.csv"
        d = pd.read_csv(p)
        d["datetime"] = pd.to_datetime(d["datetime"])
        _c[k] = d.sort_values("datetime").reset_index(drop=True)
    return _c[k]


def A(d):
    for p in (f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
              f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


rows = []
for d in DAYS:
    j = A(d)
    for t in trades(j):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        if "B3_SL" not in rs:
            continue
        sym = t["sym"]
        etf = bars(d, TAG[sym])
        hy = bars(d, "hynix")
        ex_t = pd.Timestamp(t["exit"]).tz_localize(None)
        pin = float(t["px_in"])
        fee = float(t["krw"]) / (t["q"] * pin) * 100 - (float(t["legs"][-1][2]) / pin - 1) * 100 \
            if t.get("legs") else 0.0        # net% - gross% = 비용(음수)
        r = dict(day=d, entry=str(t["entry"])[11:16], exit=str(t["exit"])[11:16], sym=sym,
                 q=t["q"], px_in=pin, px_out=float(t["legs"][-1][2]), net=t["net"], krw=t["krw"],
                 cost=fee)
        for m in (5, 10, 15, 20):
            w = etf[(etf.datetime > ex_t) & (etf.datetime <= ex_t + pd.Timedelta(minutes=m))]
            if not len(w):
                r[f"mx{m}"] = r[f"mn{m}"] = np.nan
                continue
            r[f"mx{m}"] = (w.high.max() / pin - 1) * 100 + fee      # 구간 최고 net%
            r[f"mn{m}"] = (w.low.min() / pin - 1) * 100 + fee       # 구간 최저 net%
        # 당일 끝까지 (15:20)
        w = etf[(etf.datetime > ex_t) & (etf.datetime.dt.strftime("%H%M") <= "1520")]
        r["mxeod"] = (w.high.max() / pin - 1) * 100 + fee if len(w) else np.nan
        r["mneod"] = (w.low.min() / pin - 1) * 100 + fee if len(w) else np.nan
        # 손절 시점 하이닉스 박스 위치
        h = hy[hy.datetime <= ex_t - pd.Timedelta(minutes=1)]
        h = h[h.datetime.dt.strftime("%H%M") >= "0900"]
        up = (sym == "0193T0")
        if len(h) >= 25:
            c = h.close.to_numpy(float); hh = h.high.to_numpy(float); ll = h.low.to_numpy(float)
            i0 = max(0, len(c) - 30)
            bh, bl = hh[i0:len(c) - 3].max(), ll[i0:len(c) - 3].min()
            px = c[-1]
            r["box_out"] = int(px < bl) if up else int(px > bh)   # 신호 반대방향으로 확정이탈?
            r["box_pos"] = (px - bl) / (bh - bl) if bh > bl else np.nan
        else:
            r["box_out"] = -1
            r["box_pos"] = np.nan
        rows.append(r)

d = pd.DataFrame(rows)
d.to_csv(W + "/b3sl.csv", index=False, encoding="utf-8-sig")
print("9~10월 B3_SL 손절 %d건 / 손익 %s원 / 평균 net %.2f%%"
      % (len(d), "{:+,.0f}".format(d.krw.sum()), d.net.mean()))
print("  (수수료/세금 비용 평균 %.3f%%p)" % d.cost.mean())

print("\n── 손절 후 회복률 (손절 가격이 아니라 '진입가 기준 net%' 로 측정) ──")
out = []
for m in (5, 10, 15, 20):
    mx = d[f"mx{m}"]
    out.append(dict(구간=f"{m}분",
                    본전이상=f"{(mx >= 0).mean()*100:.1f}% ({int((mx>=0).sum())}/{len(d)})",
                    _02_03=f"{((mx >= -0.2)).mean()*100:.1f}% ({int((mx>=-0.2).sum())}/{len(d)})",
                    손절가이상=f"{(mx >= d.net).mean()*100:.1f}%",
                    구간최고_평균=f"{mx.mean():+.2f}%",
                    구간최저_평균=f"{d[f'mn{m}'].mean():+.2f}%"))
print(pd.DataFrame(out).to_string(index=False))
print("\n당일끝까지: 본전이상 도달 %.1f%% (%d/%d) / 최저 평균 %.2f%% / 최저 최악 %.2f%%"
      % ((d.mxeod >= 0).mean() * 100, int((d.mxeod >= 0).sum()), len(d),
         d.mneod.mean(), d.mneod.min()))

print("\n── 손절 시점 박스 위치별 (box_out=1 : 신호 반대방향으로 박스 확정이탈 = 구조적 손절) ──")
g = d[d.box_out >= 0].groupby("box_out")
print(g.agg(건수=("net", "size"), 평균net=("net", "mean"),
            본전10분=("mx10", lambda s: (s >= 0).mean() * 100),
            본전20분=("mx20", lambda s: (s >= 0).mean() * 100),
            본전EOD=("mxeod", lambda s: (s >= 0).mean() * 100),
            최저20분=("mn20", "mean"), 최저EOD=("mneod", "mean")).to_string(float_format="%.2f"))

print("\n── 개별 (상위 20건) ──")
c = ["day", "entry", "exit", "sym", "net", "krw", "box_out", "mx5", "mx10", "mx15", "mx20", "mxeod", "mn20", "mneod"]
print(d[c].to_string(index=False, float_format="%.2f"))
