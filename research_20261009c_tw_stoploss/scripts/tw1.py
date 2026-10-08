"""TIME_WINDOW_STOP_LOSS 33건 해부 — 85영업일. READ-ONLY.

모든 포지션의 1분봉 경로로 MAE/MFE/peak 도달시각을 정확 계산한다.
손절선 강화는 MAE 로 **정확계산**이 가능하다(기존 메모 project-faithful-engine-not-in-repo).
다만 진입집합/슬롯 연쇄효과는 재생이 필요하므로, 여기 수치는 '1차 산술'이다.
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
pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 200)

DAYS = [l.strip() for l in open(W + "/all85.txt") if l.strip()]
TAG = {"0193T0": "long", "0197X0": "inverse"}
_c = {}


def bars(day, sym):
    k = (day, sym)
    if k not in _c:
        p = f"{SPD}/replay_{day}_{TAG[sym]}_1m.csv"
        if os.path.exists(p):
            d = pd.read_csv(p)
            d["datetime"] = pd.to_datetime(d["datetime"])
            if getattr(d["datetime"].dt, "tz", None) is not None:
                d["datetime"] = d["datetime"].dt.tz_localize(None)
            _c[k] = d.sort_values("datetime").reset_index(drop=True)
        else:
            _c[k] = None
    return _c[k]


def A(d):
    for p in (f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


rows, nobar = [], 0
for d in DAYS:
    j = A(d)
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in (j.get("entries") or [])}
    for t in trades(j):
        e = ent.get(pd.Timestamp(t["entry"]).strftime("%H:%M:%S"), {})
        b = bars(d, t["sym"])
        e0 = pd.Timestamp(t["entry"]).tz_localize(None)
        e1 = pd.Timestamp(t["exit"]).tz_localize(None)
        cost = t["net"] - (float(t["legs"][-1][2]) / t["px_in"] - 1) * 100
        r = dict(day=d, sym=t["sym"], dir=("UP" if t["sym"] == "0193T0" else "DN"),
                 entry=e0, exit=e1, hhmm=e0.strftime("%H:%M"), hour=e0.hour,
                 q=t["q"], px=t["px_in"], net=t["net"], krw=t["krw"], hold=t["hold_min"],
                 reason="+".join(dict.fromkeys(t["reasons"])),
                 regime=e.get("regime"), snap=e.get("snap_reason"), cost=cost)
        if b is None:
            nobar += 1
            r.update(mae=np.nan, mfe=np.nan, t_mae=np.nan, t_mfe=np.nan, mfe_before_mae=np.nan)
        else:
            w = b[(b.datetime >= e0) & (b.datetime <= e1)]
            if len(w):
                lo = (w.low.to_numpy(float) / t["px_in"] - 1) * 100 + cost
                hi = (w.high.to_numpy(float) / t["px_in"] - 1) * 100 + cost
                i_lo, i_hi = int(np.argmin(lo)), int(np.argmax(hi))
                r.update(mae=lo.min(), mfe=hi.max(),
                         t_mae=(w.datetime.iloc[i_lo] - e0).total_seconds() / 60,
                         t_mfe=(w.datetime.iloc[i_hi] - e0).total_seconds() / 60,
                         mfe_before_mae=hi[:i_lo + 1].max())
            else:
                nobar += 1
                r.update(mae=np.nan, mfe=np.nan, t_mae=np.nan, t_mfe=np.nan, mfe_before_mae=np.nan)
        rows.append(r)
T = pd.DataFrame(rows)
T.to_csv(W + "/all85_paths.csv", index=False, encoding="utf-8-sig")


def sec(t):
    print("\n" + "=" * 112 + "\n" + t + "\n" + "=" * 112)


print("85일 거래 %d건 / 합계 %s / 1분봉 없음 %d건" %
      (len(T), "{:+,.0f}".format(T.krw.sum()), nobar))

TW = T[T.reason.str.contains("TIME_WINDOW_STOP_LOSS")]
sec("1. TIME_WINDOW_STOP_LOSS %d건 — 누구인가" % len(TW))
print("합계 %s / 평균 net %.2f%% / 평균보유 %.0f분" %
      ("{:+,.0f}".format(TW.krw.sum()), TW.net.mean(), TW.hold.mean()))
for col in ("regime", "snap", "dir"):
    g = TW.groupby(col).agg(건수=("krw", "size"), 합계=("krw", "sum"), 평균net=("net", "mean"))
    print("\n[%s]" % col)
    print(g.to_string(float_format="{:,.1f}".format))
print("\n[진입 시간대]")
g = TW.groupby("hour").agg(건수=("krw", "size"), 합계=("krw", "sum"), 평균net=("net", "mean"), 평균보유=("hold", "mean"))
print(g.to_string(float_format="{:,.1f}".format))

sec("2. 죽기 전에 수익을 봤는가 (MFE)")
print("  MFE 분포:")
bins = [-99, 0, 0.3, 0.6, 1.0, 1.5, 99]
lab = ["<=0", "0~0.3", "0.3~0.6", "0.6~1.0", "1.0~1.5", ">1.5"]
TW2 = TW.dropna(subset=["mfe"]).copy()
TW2["mfe_bin"] = pd.cut(TW2.mfe, bins=bins, labels=lab)
g = TW2.groupby("mfe_bin", observed=False).agg(건수=("krw", "size"), 합계=("krw", "sum"),
                                               평균net=("net", "mean"), 평균_t_mfe=("t_mfe", "mean"))
print(g.to_string(float_format="{:,.1f}".format))
print("\n  MFE <= 0 (한 번도 플러스를 못 본) : %d건 %s" %
      (int((TW2.mfe <= 0).sum()), "{:+,.0f}".format(TW2.loc[TW2.mfe <= 0, "krw"].sum())))
print("  MFE >= 1.0%% 찍고 죽은 거래      : %d건 %s (평균 peak %.2f%% at %.0f분)" %
      (int((TW2.mfe >= 1.0).sum()), "{:+,.0f}".format(TW2.loc[TW2.mfe >= 1.0, "krw"].sum()),
       TW2.loc[TW2.mfe >= 1.0, "mfe"].mean() if (TW2.mfe >= 1.0).any() else 0,
       TW2.loc[TW2.mfe >= 1.0, "t_mfe"].mean() if (TW2.mfe >= 1.0).any() else 0))

sec("3. 손절선을 조이면? (전체 152건 MAE 기준 정확계산, 연쇄효과 제외)")
P = T.dropna(subset=["mae"]).copy()
base = float(P.krw.sum())
print("  대상 %d건 / 현행 합계 %s" % (len(P), "{:+,.0f}".format(base)))
out = []
for lvl in (-0.8, -1.0, -1.1, -1.2, -1.3, -1.4, -1.5):
    hit = P[P.mae <= lvl]
    keep = P[P.mae > lvl]
    cutk = float(sum(lvl / 100 * hit.q * hit.px))
    tot = float(keep.krw.sum()) + cutk
    out.append(dict(손절선=f"{lvl:.1f}%", 도달=len(hit), 도달건_현행=float(hit.krw.sum()),
                    도달건_조이면=cutk, 전체=tot, Δ=tot - base,
                    살린손실=float(hit.loc[hit.krw < 0, "krw"].sum()),
                    죽인수익=float(hit.loc[hit.krw > 0, "krw"].sum()),
                    수익건수=int((hit.krw > 0).sum())))
o = pd.DataFrame(out)
print(o.to_string(index=False, formatters={c: "{:+,.0f}".format for c in
                                           ["도달건_현행", "도달건_조이면", "전체", "Δ", "살린손실", "죽인수익"]}))

sec("4. TW 손절 33건만 조였다면 (승자 훼손 없음 — 상한 추정)")
TWp = TW.dropna(subset=["mae"])
for lvl in (-1.0, -1.1, -1.2, -1.3):
    cut = float(sum(lvl / 100 * TWp.q * TWp.px))
    print("  %.1f%% 로 조이면 : %s -> %s  (Δ %s)  ※ 같은 선에 닿는 승자는 포함 안 한 상한값"
          % (lvl, "{:+,.0f}".format(TWp.krw.sum()), "{:+,.0f}".format(cut),
             "{:+,.0f}".format(cut - TWp.krw.sum())))

sec("5. 같은 손절선에 닿는 '승자' — 조이기의 비용")
for lvl in (-1.0, -1.2, -1.4):
    w = P[(P.mae <= lvl) & (P.krw > 0)]
    print("  %.1f%% 에 닿은 수익거래 %d건 / 실제 %s -> 잘리면 %s  (손해 %s)"
          % (lvl, len(w), "{:+,.0f}".format(w.krw.sum()),
             "{:+,.0f}".format(float(sum(lvl / 100 * w.q * w.px))),
             "{:+,.0f}".format(float(sum(lvl / 100 * w.q * w.px)) - w.krw.sum())))

sec("6. TW 손절 33건 개별")
c = ["day", "hhmm", "dir", "regime", "snap", "net", "krw", "hold", "mae", "mfe", "t_mfe", "mfe_before_mae"]
print(TW[c].sort_values("krw").to_string(index=False, float_format="{:,.2f}".format))
