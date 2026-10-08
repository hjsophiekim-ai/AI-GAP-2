"""BOX vs BREAKOUT 판별력 분석. READ-ONLY.

train = 9월(0903~0930), test = 10월(1001~1008). 임계값은 train 분위수로만 정하고
(탐색 금지) test 는 손대지 않는다.
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 80)

d = pd.read_csv(W + "/feat.csv")
d["day"] = d.day.astype(str)
d["t"] = pd.to_datetime(d.dec).dt.strftime("%H:%M")
d["mon"] = np.where(d.day < "20261001", "9월", "10월")

# ── 결과변수 ────────────────────────────────────────────────────────────────
#  y_trade : 실제 체결된 거래의 실현손익 부호 (production 원장, 근거 확실)
#  y_run   : 하이닉스가 신호방향 +0.5% 를 -0.5% 보다 먼저 찍었는가
#            (0.5% = B3 손절 1.0% / 2배 ETF. 오늘 데이터에서 고른 값이 아님)
d["y_trade"] = np.where(d.net_krw > 0, 1, np.where(d.net_krw < 0, 0, np.nan))
d["y_run"] = np.where(d.first05 == 1, 1.0, np.where(d.first05 == -1, 0.0, np.nan))

# a priori 방향: + = 값이 클수록 '진짜 돌파' 라는 가설
FEATS = {
    "brk20": +1, "brk30": +1, "brk40": +1,
    "nout20": +1, "nout30": +1, "nout40": +1,
    "net20": +1, "net30": +1, "net40": +1,
    "peff20": +1, "peff30": +1, "peff40": +1,
    "nover20": +1, "nover30": +1, "nover40": +1,
    "box_rng20": 0, "box_rng30": 0, "box_rng40": 0,
    "hhll": +1, "ema20_sl": +1, "ema50_sl": +1, "ema_sep": +1,
    "gap": +1, "gap_d": +1, "gap_dd": +1,
}
POST = {"reent1": -1, "reent2": -1, "reent3": -1, "ext1": +1, "ext2": +1, "ext3": +1}


def auc(x, y):
    m = (~pd.isna(x)) & (~pd.isna(y))
    x, y = np.asarray(x)[m], np.asarray(y)[m]
    p, n = x[y == 1], x[y == 0]
    if len(p) == 0 or len(n) == 0:
        return np.nan, len(p), len(n)
    r = pd.Series(np.concatenate([p, n])).rank().to_numpy()
    a = (r[:len(p)].sum() - len(p) * (len(p) + 1) / 2) / (len(p) * len(n))
    return a, len(p), len(n)


def section(title):
    print("\n" + "=" * 100 + "\n" + title + "\n" + "=" * 100)


# ── 0. 표본 ────────────────────────────────────────────────────────────────
section("0. 표본")
print("승인(T+3) 신호 %d건 / %d영업일" % (len(d), d.day.nunique()))
print(d.groupby(["mon", "result"]).size().to_string())
print("\n실현손익 있는 체결 %d건 (9월 %d / 10월 %d)" %
      (d.net_krw.notna().sum(), d[(d.mon == "9월") & d.net_krw.notna()].shape[0],
       d[(d.mon == "10월") & d.net_krw.notna()].shape[0]))

# ── 1. 오늘 4(6)건 ─────────────────────────────────────────────────────────
section("1. 2026-10-08 승인신호 피처")
t = d[d.day == "20261008"].copy()
lab = {"09:30": "①09:24 UP_RED (HISTORY_GAP, 미진입)",
       "09:42": "②09:36 DOWN_BLUE  ← 실제진입·손절",
       "10:18": "③10:12 UP_RED     ← 실제진입·손절",
       "11:12": "④11:06 DOWN_BLUE  ← 진짜 하락탈출(미진입)",
       "13:15": "⑤13:09 UP_RED     ← 진짜 상승탈출(미진입)",
       "14:36": "⑥14:30 DOWN_BLUE (슬롯캡, 미진입)"}
t["lab"] = t.t.map(lab)
show = ["lab", "px", "box_rng30", "brk30", "nout30", "net30", "peff30", "nover30", "hhll",
        "ema20_sl", "ema50_sl", "ema_sep", "gap", "gap_d", "gap_dd",
        "reent1", "reent2", "reent3", "ext1", "ext2", "ext3",
        "mfe30", "mae30", "fwdnet30", "mfe60", "mae60", "fwdnet60", "mfeeod", "maeeod",
        "first05", "net_krw"]
print(t[show].set_index("lab").T.to_string(float_format=lambda x: "%.3f" % x))

# ── 2. 판별력 (train=9월) ──────────────────────────────────────────────────
section("2. 단일변수 판별력 — AUC (train 9월 / test 10월)")
tr, te = d[d.mon == "9월"], d[d.mon == "10월"]
rows = []
for f, sgn in list(FEATS.items()) + list(POST.items()):
    for yn in ("y_trade", "y_run"):
        a1, p1, n1 = auc(tr[f], tr[yn])
        a2, p2, n2 = auc(te[f], te[yn])
        if sgn < 0:
            a1, a2 = 1 - a1, 1 - a2
        rows.append(dict(feat=f, y=yn, auc_tr=a1, n_tr=p1 + n1, auc_te=a2, n_te=p2 + n2,
                         dev=abs(a1 - 0.5)))
r = pd.DataFrame(rows)
for yn in ("y_trade", "y_run"):
    s = r[r.y == yn].sort_values("dev", ascending=False)
    print("\n[%s]  (AUC 0.5 = 무판별. a priori 부호 적용 후)" % yn)
    print(s[["feat", "auc_tr", "n_tr", "auc_te", "n_te"]].head(14).to_string(
        index=False, float_format=lambda x: "%.3f" % x))

def fmt(x):
    if isinstance(x, float):
        return ("{:,.0f}".format(x)) if abs(x) >= 1000 else ("{:.3f}".format(x))
    return str(x)


# ── 3. 사전등록 게이트 ──────────────────────────────────────────────────────
section("3. BREAKOUT-GATE 후보 — 임계값 = 9월 분위수 고정 (탐색 없음)")
best = r[r.y == "y_run"].sort_values("dev", ascending=False)
cand = [f for f in best.feat if f in FEATS and FEATS[f] != 0][:3]
print("선정: 9월 AUC 상위이면서 a priori 부호가 있는 진입시점 변수 ->", cand)
out = []
for f in cand + ["ext3"]:
    sgn = FEATS.get(f, POST.get(f))
    for q, qn in ((0.50, "9월중앙값"), (0.3333, "9월하위1/3")):
        thr = tr[f].quantile(q if sgn > 0 else 1 - q)
        keep = (d[f] >= thr) if sgn > 0 else (d[f] <= thr)
        for mon in ("9월", "10월"):
            ex = (d.mon == mon) & d.net_krw.notna()
            bl, kp = ex & ~keep, ex & keep
            out.append({"변수": f, "임계": qn, "thr": round(thr, 3), "월": mon,
                        "거래": int(ex.sum()), "차단": int(bl.sum()),
                        "차단손익": d.loc[bl, "net_krw"].sum(),
                        "잔존손익": d.loc[kp, "net_krw"].sum(),
                        "원본손익": d.loc[ex, "net_krw"].sum(),
                        "FP(수익인데차단)": int((bl & (d.net_krw > 0)).sum()),
                        "FN(손실인데통과)": int((kp & (d.net_krw < 0)).sum())})
print(pd.DataFrame(out).to_string(index=False, formatters={c: fmt for c in
      ["차단손익", "잔존손익", "원본손익"]}))

# ── 4. 9월 승/패 분포 겹침 ─────────────────────────────────────────────────
section("4. 9월 체결거래 승/패별 진입시점 피처 분포 (중앙값 / 겹침 확인)")
g = tr[tr.net_krw.notna()].groupby(tr.net_krw > 0)[
    ["brk30", "nout30", "net30", "peff30", "nover30", "box_rng30", "hhll",
     "ema20_sl", "ema50_sl", "gap_d", "ext3"]].median().T
g.columns = ["손실(n=%d)" % (tr.net_krw < 0).sum(), "수익(n=%d)" % (tr.net_krw > 0).sum()]
print(g.to_string(float_format=lambda x: "%.3f" % x))

# ── 5. 오늘 4건 순위 역전 ──────────────────────────────────────────────────
section("5. 오늘 핵심 4건 — '돌파강도' 변수로 줄세우면?")
k = d[(d.day == "20261008") & d.t.isin(["09:42", "10:18", "11:12", "13:15"])].copy()
k["라벨"] = k.t.map({"09:42": "가짜(실제손절)", "10:18": "가짜(실제손절)",
                     "11:12": "진짜 하락탈출", "13:15": "진짜 상승탈출"})
for f in ["brk30", "brk40", "nout30", "net30", "peff30", "nover30", "ema50_sl", "gap_d", "ext3"]:
    o = k.sort_values(f, ascending=False)
    print("%-9s 강한순: %s" % (f, "  >  ".join("%s %s(%.3f)" % (x.t, x.라벨, getattr(x, f))
                                               for x in o.itertuples())))

# ── 6. 유의성 (permutation) ────────────────────────────────────────────────
section("6. 9월 AUC 가 우연인가 — permutation p-value (2,000회, seed 20261008)")
rng = np.random.default_rng(20261008)
for f in cand + ["ext3"]:
    for yn in ("y_run", "y_trade"):
        sub = tr[[f, yn]].dropna()
        if sub[yn].nunique() < 2:
            continue
        a0 = auc(sub[f], sub[yn])[0]
        sgn = FEATS.get(f, POST.get(f))
        if sgn < 0:
            a0 = 1 - a0
        yv = sub[yn].to_numpy()
        null = np.array([auc(sub[f], rng.permutation(yv))[0] for _ in range(2000)])
        if sgn < 0:
            null = 1 - null
        print("  %-8s %-8s AUC %.3f  p(한쪽) = %.3f  n=%d" %
              (f, yn, a0, (null >= a0).mean(), len(sub)))

# ── 7. 날짜별 ──────────────────────────────────────────────────────────────
section("7. 날짜별 (10/01, 10/02, 10/06, 10/07, 10/08)")
for day in ["20261001", "20261002", "20261006", "20261007", "20261008"]:
    s = d[d.day == day]
    if not len(s):
        print("\n[%s] 데이터 없음" % day); continue
    ex = s[s.net_krw.notna()]
    print("\n[%s] 승인신호 %d건 / 체결 %d건 / 실현손익 %s" %
          (day, len(s), len(ex), fmt(float(ex.net_krw.sum())) if len(ex) else "-"))
    c = ["t", "dir", "result", "brk30", "nout30", "peff30", "nover30", "ema50_sl",
         "gap_d", "ext3", "first05", "fwdnet60", "net_krw"]
    print(s[c].to_string(index=False, float_format=lambda x: "%.3f" % x))

d.to_csv(W + "/feat2.csv", index=False, encoding="utf-8-sig")

# ── 8. ext3 게이트의 실제 비용: 3분 뒤 들어가면 진입가가 얼마나 나빠지나 ──
section("8. '3분 기다렸다 확인 후 진입' 게이트의 체결비용 (ETF 실제 1분봉)")
import glob, json
ROOTp = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
QTY = {}
for dd in [ROOTp + "/research_20261008_peaklock/out", W + "/out"]:
    for p in sorted(glob.glob(dd + "/REAL_UPFASTRS_*.json")):
        j = json.load(open(p, encoding="utf-8"))
        for e in j["ex"]:
            if e["side"] == "BUY" and e["signal_id"]:
                QTY.setdefault(e["signal_id"], (int(e["executed_qty"]), float(e["executed_price"]), e["symbol"]))
QTY["20261008_093600_DOWN_BLUE:TW2_3SLOT_CONFIRM"] = (1510, 6225.0, "0197X0")
QTY["20261008_101200_UP_RED:TW2_3SLOT_CONFIRM"] = (838, 10405.0, "0193T0")

ETF = {}
def etf(day, sym):
    k = (day, sym)
    if k in ETF:
        return ETF[k]
    tag = "long" if sym == "0193T0" else "inverse"
    for pp in (ROOTp + "/research_20261004_chop_staged/wk/data/replay_%s_%s_1m.csv" % (day, tag),
               ROOTp + "/data/cache/replay_%s_%s_1m.csv" % (day, tag),
               W + "/data/naver_%s_%s_1m.csv" % (day, tag)):
        if os.path.exists(pp):
            x = pd.read_csv(pp)
            x["datetime"] = pd.to_datetime(x["datetime"])
            ETF[k] = x
            return x
    ETF[k] = None
    return None

thr = tr["ext3"].quantile(0.50)
rows = []
for _, s in d[d.net_krw.notna()].iterrows():
    q = QTY.get(s.sid)
    if not q:
        continue
    qty, bpx, sym = q
    x = etf(s.day, sym)
    dec = pd.Timestamp(s.dec)
    if x is None:
        continue
    lat = x[(x.datetime >= dec + pd.Timedelta(minutes=3)) & (x.datetime <= dec + pd.Timedelta(minutes=5))]
    if not len(lat):
        continue
    px3 = float(lat.close.iloc[0])
    rows.append(dict(mon=s.mon, day=s.day, t=s.t, sid=s.sid, ext3=s.ext3, qty=qty,
                     px0=bpx, px3=px3, slip=(px3 - bpx) * qty, net=s.net_krw,
                     keep=bool(s.ext3 >= thr)))
e3 = pd.DataFrame(rows)
print("ext3 임계 = 9월 중앙값 %.3f%%   매칭된 체결 %d/%d건" % (thr, len(e3), int(d.net_krw.notna().sum())))
for mon in ("9월", "10월"):
    m = e3[e3.mon == mon]
    kp = m[m.keep]
    print("\n[%s] 원본 %d건 %s" % (mon, len(m), fmt(float(m.net.sum()))))
    print("  통과 %d건: 원본손익 %s  진입가 악화 %s  ->  보정손익 %s" %
          (len(kp), fmt(float(kp.net.sum())), fmt(float(-kp.slip.sum())),
           fmt(float(kp.net.sum() - kp.slip.sum()))))
    print("  차단 %d건: 회피손익 %s" % (len(m) - len(kp), fmt(float(m[~m.keep].net.sum()))))
    print("  게이트 후 합계 %s  (원본 대비 %s)" %
          (fmt(float(kp.net.sum() - kp.slip.sum())), fmt(float(kp.net.sum() - kp.slip.sum() - m.net.sum()))))
print("\n통과거래 진입가 악화 상세:")
print(e3[e3.keep][["day", "t", "ext3", "qty", "px0", "px3", "slip", "net"]].to_string(
    index=False, formatters={"slip": fmt, "net": fmt}))
