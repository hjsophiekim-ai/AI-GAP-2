"""T+3 직후 눌림 → reclaim 진단 (2026-11-02, research only).

입력: 재생 산출 {ARM3_OUT}/L85_D_<day>.json (CHOP 모드, MU Assist OFF, 85일)
      1분봉 {REPLAY_SP}/data/replay_<day>_{long,inverse}_1m.csv  (bar 시각 = 시작)
정의 (사전 고정, 탐색하지 않음):
  T+3      = 플래그봉 시작 + 6분 (sid 의 HHMMSS = 플래그봉 시작)
  ref      = T+3 직전 완성 1분봉 종가 (T+3 시점에 알 수 있는 가격)
  STH      = 확인봉 3분(T+3-3분 ~ T+3) 1분봉 high 최대 — '단기 고점'
  창        = T+3 ~ T+3+6분 의 완성 1분봉 6개
  pullback = 창 안에서 low < ref 가 한 번이라도
  reclaim  = pullback 이후 같은 창 안에서 1분봉 close >= ref (STH 판정은 close >= STH)
  분류      N   = 창 안에서 눌림 없음
           PR  = 눌림 + 6분 안 reclaim
           PNR = 눌림 + 6분 안 reclaim 없음
  승패      = 그 포지션 실현손익 > 0 (수수료 0.0036% 양방향)
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
SP = Path(os.environ["REPLAY_SP"])
OUT = Path(os.environ["ARM3_OUT"])
FEE = 0.000036
LONG = "0193T0"
WIN = 6
#: ETF 1분봉 실데이터 시작일. 06/19~07/31 은 합성(시가 13,300 고정·거래량 수열 복제) 또는
#: 타종목 가격대(19,000~44,000)라 체결가 분석에 쓸 수 없다 (2026-11-02 확인).
REAL_FROM = "20260803"


def _days():
    return sorted({f.name.split("_")[1] for f in (SP / "data").glob("replay_*_long_1m.csv")})


def bars(day, sym):
    """당일 + 전일 (ATR14 가 장초반에도 계산되도록 — 플래그 엔진처럼 전일봉 포함)."""
    tag = "long" if sym == LONG else "inverse"
    ds = _days()
    use = [x for x in ds if REAL_FROM <= x < day][-1:] + [day]   # 합성/타종목 구간 전일봉 제외
    out = []
    for x in use:
        d = pd.read_csv(SP / "data" / f"replay_{x}_{tag}_1m.csv")
        d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19])
        d = d[(d.datetime.dt.time >= pd.Timestamp("09:00").time()) & (d.datetime.dt.time < pd.Timestamp("15:30").time())]
        out.append(d)
    return pd.concat(out).drop_duplicates("datetime").set_index("datetime").sort_index()


def episodes(js, day):
    """BUY 1건 -> 전량 매도까지 1 포지션."""
    eps, cur = [], None
    ents = {e["t"]: e for e in js.get("entries", [])}
    for o in js["orders"]:
        if o["t"][:10].replace("-", "") != day:
            continue
        if o["side"] == "BUY":
            if cur is not None and cur["left"] > 0:       # 추가매수(현행엔 없음)
                cur["buys"].append(o); cur["left"] += o["qty"]; continue
            cur = dict(sym=o["sym"], buys=[o], sells=[], left=o["qty"], ent=ents.get(o["t"], {}))
            eps.append(cur)
        else:
            if cur is None:
                continue
            cur["sells"].append(o); cur["left"] -= o["qty"]
    return eps


def analyse(day, ep, b):
    buy = ep["buys"][0]
    te = pd.Timestamp(buy["t"][:19])
    q, e = buy["qty"], float(buy["px"])
    sells = ep["sells"]
    if not sells:
        return None
    tx = pd.Timestamp(sells[-1]["t"][:19])
    pnl = sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e) for s in sells)
    sid = ep["ent"].get("sid", "")
    try:
        flag = pd.Timestamp(f"{sid[:8]} {sid[9:11]}:{sid[11:13]}:{sid[13:15]}")
    except Exception:
        flag = None
    t3 = flag + pd.Timedelta(minutes=6) if flag is not None else te.floor("min")
    pre = b[b.index + pd.Timedelta(minutes=1) <= t3]
    ref = float(pre["close"].iloc[-1])
    conf = b[(b.index >= t3 - pd.Timedelta(minutes=3)) & (b.index < t3)]
    sth = float(conf["high"].max()) if len(conf) else ref
    w = b[(b.index >= t3) & (b.index < t3 + pd.Timedelta(minutes=WIN))]
    pb_i = next((i for i, (_, r) in enumerate(w.iterrows()) if r.low < ref), None)
    rc_t = rc_sth_t = None
    if pb_i is not None:
        for ts, r in w.iloc[pb_i:].iterrows():
            if rc_t is None and r.close >= ref:
                rc_t = ts + pd.Timedelta(minutes=1)
            if rc_sth_t is None and r.close >= sth:
                rc_sth_t = ts + pd.Timedelta(minutes=1)
    cls = "N" if pb_i is None else ("PR" if rc_t is not None else "PNR")
    # 확장 판정 (사전 고정): ref - 6분(플래그+확인봉) 최저 >= 1.0 x ATR14(완성 3분봉)
    day0 = b[b.index + pd.Timedelta(minutes=1) <= t3]
    b3 = day0.resample("3min", origin="start_day").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    b3 = b3[b3.index + pd.Timedelta(minutes=3) <= t3]
    atr = np.nan
    if len(b3) >= 5:
        pc = b3["close"].shift(1)
        tr = pd.concat([b3.high - b3.low, (b3.high - pc).abs(), (b3.low - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.iloc[-14:].mean())
    six = b[(b.index >= t3 - pd.Timedelta(minutes=6)) & (b.index < t3)]
    ext_amt = ref - float(six["low"].min()) if len(six) else np.nan
    ext_atr = ext_amt / atr if atr == atr and atr > 0 else np.nan
    extended = bool(ext_atr >= 1.0) if ext_atr == ext_atr else False
    # ── BURST / GRIND 특성: T+3 직전 완성 3분봉 3개 (보유 ETF 상승방향 = RED/BLUE 대칭) ──
    b3d = b3[b3.index.normalize() == t3.normalize()]          # 당일봉만 (갭 제외)
    k3 = b3d.iloc[-3:] if len(b3d) >= 2 else None             # 09:06 T+3 는 당일 2봉
    bf = {}
    if k3 is not None:
        o3 = day0.resample("3min", origin="start_day").agg({"open": "first"}).dropna()
        o3 = o3.loc[k3.index, "open"]
        body = (k3["close"] - o3) / o3 * 100
        rng_ = (k3["high"] - k3["low"]) / o3 * 100
        hi, lo = float(k3["high"].max()), float(k3["low"].min())
        flag_close = float(k3["close"].iloc[-2])          # 플래그봉 종가 (마지막=확인봉)
        bf = dict(
            cum3=(float(k3["close"].iloc[-1]) / float(o3.iloc[0]) - 1) * 100,
            nb=len(k3), body1=body.iloc[0] if len(k3) == 3 else np.nan,
            body2=body.iloc[-2], body3=body.iloc[-1],
            rng1=rng_.iloc[0] if len(k3) == 3 else np.nan, rng2=rng_.iloc[-2], rng3=rng_.iloc[-1],
            n_dir=int((body > 0).sum()),
            last_br=float(body.iloc[-1] / rng_.iloc[-1]) if rng_.iloc[-1] > 0 else 0.0,
            last_big=bool(abs(body.iloc[-1]) >= abs(body).max() and body.iloc[-1] > 0),
            pos3=(ref - lo) / (hi - lo) if hi > lo else 0.5,
            flag_prog=(ref / flag_close - 1) * 100,
        )
        bf["burst"] = bool(bf["n_dir"] == len(k3) and bf["last_big"])
    after = b[(b.index >= te.floor("min"))]
    for k in (1, 3, 6, 9):
        seg = after.iloc[:k]
        bf[f"mae{k}"] = (float(seg["low"].min()) / e - 1) * 100
        bf[f"mfe{k}"] = (float(seg["high"].max()) / e - 1) * 100
    lows = {k: (float(after.iloc[:k]["low"].min()) / e - 1) * 100 for k in (1, 3, 6, 9)}
    hold = b[(b.index >= te.floor("min")) & (b.index < tx)]
    if hold.empty:
        hold = after.iloc[:1]
    mfe = (float(hold["high"].max()) / e - 1) * 100
    mae = (float(hold["low"].min()) / e - 1) * 100
    # 창 이후 첫 reclaim 시각(6분 넘어서라도) — 참고
    rc_any = None
    if pb_i is not None:
        z = b[(b.index >= w.index[pb_i]) & (b.index < tx)]
        hit = z[z.close >= ref]
        rc_any = hit.index[0] + pd.Timedelta(minutes=1) if len(hit) else None
    held_min = (tx - te).total_seconds() / 60
    return dict(
        day=day, month=day[:6], sym="L" if ep["sym"] == LONG else "I",
        flag=flag.strftime("%H:%M") if flag is not None else "", t3=t3.strftime("%H:%M"),
        entry=te.strftime("%H:%M:%S"), e_brk=":E_BRK" in sid, regime=ep["ent"].get("regime"),
        ref=ref, entry_px=e, slip_pct=(e / ref - 1) * 100, qty=q,
        low1=lows[1], low3=lows[3], low6=lows[6], low9=lows[9],
        pb6_ref=(float(w["low"].min()) / ref - 1) * 100 if len(w) else np.nan,
        cls=cls, ext_atr=ext_atr, extended=extended, near_sth=(ref / sth - 1) * 100, rc_min=(rc_t - t3).total_seconds() / 60 if rc_t is not None else np.nan,
        rc_sth=rc_sth_t is not None,
        rc_any_min=(rc_any - t3).total_seconds() / 60 if rc_any is not None else np.nan,
        exit=tx.strftime("%H:%M:%S"), held_min=held_min, exit_before_win=held_min < WIN + (t3 - te).total_seconds() / 60,
        reason=";".join(sorted({a.split(":")[0] for s in sells for a in s.get("acts", [])})),
        pnl=pnl, win=pnl > 0, mfe=mfe, mae=mae, **bf,
    )


def pf(x):
    g, l = x[x > 0].sum(), -x[x < 0].sum()
    return g / l if l else np.inf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="L85_D")
    ap.add_argument("--csv", default=str(Path(__file__).resolve().parents[1] / "out" / "diag_trades.csv"))
    a = ap.parse_args()
    rows = []
    files = [f for f in sorted(OUT.glob(f"{a.tag}_2026*.json")) if f.stem.split("_")[-1] >= REAL_FROM]
    for f in files:
        day = f.stem.split("_")[-1]
        js = json.loads(f.read_text(encoding="utf-8"))
        cache = {}
        for ep in episodes(js, day):
            if ep["sym"] not in cache:
                cache[ep["sym"]] = bars(day, ep["sym"])
            r = analyse(day, ep, cache[ep["sym"]])
            if r:
                rows.append(r)
    d = pd.DataFrame(rows)
    Path(a.csv).parent.mkdir(parents=True, exist_ok=True)
    d.to_csv(a.csv, index=False, encoding="utf-8-sig")
    print(f"[{a.tag}] {len(files)}일 {len(d)}거래  총손익 {d.pnl.sum():+,.0f}  PF {pf(d.pnl):.3f}  승 {int(d.win.sum())}/패 {int((~d.win).sum())}")
    W, L = d[d.win], d[~d.win]

    print("\n[1] 진입 후 최저가(진입가 대비 %) — 중앙값 / 평균")
    for c in ["low1", "low3", "low6", "low9", "pb6_ref"]:
        print(f"  {c:8s}  승 {W[c].median():+.2f} / {W[c].mean():+.2f}    패 {L[c].median():+.2f} / {L[c].mean():+.2f}")
    print(f"  진입 체결가 - ref(T+3 종가) 평균 {d.slip_pct.mean():+.3f}%")

    print("\n[2] 6분 창 분류 × 승패")
    ct = pd.crosstab(d.cls, d.win, margins=True)
    print(ct.to_string())
    for c in ["N", "PR", "PNR"]:
        s = d[d.cls == c]
        if len(s):
            print(f"  {c:4s} {len(s):3d}건  승률 {s.win.mean()*100:5.1f}%  손익 {s.pnl.sum():+13,.0f}  거래당 {s.pnl.mean():+10,.0f}  PF {pf(s.pnl):.2f}  평균MFE {s.mfe.mean():+.2f}")

    print("\n[3] 눌린 거래 중 6분 안 reclaim 비율")
    for lab, s in (("승", W), ("패", L)):
        p = s[s.cls != "N"]
        print(f"  {lab}: 눌림 {len(p)}/{len(s)}  reclaim {int((p.cls=='PR').sum())}/{len(p)} = {((p.cls=='PR').mean()*100 if len(p) else 0):.1f}%"
              f"   STH reclaim {int(p.rc_sth.sum())}/{len(p)}   3~6분 reclaim {int(((p.rc_min>=3)&(p.rc_min<=6)).sum())}")
    print(f"  패자 중 reclaim 실패(PNR) {int((L.cls=='PNR').sum())}/{len(L)} = {(L.cls=='PNR').mean()*100:.1f}%"
          f"   (승자 중 PNR {(W.cls=='PNR').mean()*100:.1f}%)")

    # 구분력: P(win|PR) - P(win|PNR), 거래 부트스트랩 95% CI
    rng = np.random.default_rng(0)
    pr, pnr = d[d.cls == "PR"].win.to_numpy(), d[d.cls == "PNR"].win.to_numpy()
    if len(pr) and len(pnr):
        bs = [rng.choice(pr, len(pr)).mean() - rng.choice(pnr, len(pnr)).mean() for _ in range(5000)]
        print(f"\n[4] 구분력  승률(PR) {pr.mean()*100:.1f}% − 승률(PNR) {pnr.mean()*100:.1f}% = {(pr.mean()-pnr.mean())*100:+.1f}%p"
              f"  95%CI [{np.percentile(bs,2.5)*100:+.1f}, {np.percentile(bs,97.5)*100:+.1f}]")
        from scipy.stats import fisher_exact, mannwhitneyu
        tab = [[int(pr.sum()), int(len(pr) - pr.sum())], [int(pnr.sum()), int(len(pnr) - pnr.sum())]]
        print(f"      Fisher p = {fisher_exact(tab)[1]:.3f}   (PR vs PNR 손익 Mann-Whitney p = "
              f"{mannwhitneyu(d[d.cls=='PR'].pnl, d[d.cls=='PNR'].pnl).pvalue:.3f})")
    # AUC: reclaim 여부 하나로 승패를 맞히는 힘 (눌린 거래만)
    p = d[d.cls != "N"]
    if len(p) and p.win.nunique() == 2:
        from sklearn.metrics import roc_auc_score
        print(f"      AUC(reclaim→승, 눌린 거래 {len(p)}건) = {roc_auc_score(p.win, (p.cls=='PR').astype(int)):.3f}"
              f"   AUC(STH reclaim) = {roc_auc_score(p.win, p.rc_sth.astype(int)):.3f}")

    print("\n[5] 6분 안에 이미 청산된 거래(분류가 의미 없는 거래)")
    s = d[d.exit_before_win]
    print(f"  {len(s)}건  손익 {s.pnl.sum():+,.0f}  " + ", ".join(f"{r.day[4:]} {r.entry[:5]} {r.cls} {r.pnl:+,.0f}" for r in s.itertuples()))

    print("\n[6] 월별 반복성 (PR/PNR 승률)")
    for m, g in d.groupby("month"):
        a1, b1 = g[g.cls == "PR"], g[g.cls == "PNR"]
        print(f"  {m}  거래 {len(g):3d}  PR {len(a1):2d}건 승률 {a1.win.mean()*100 if len(a1) else float('nan'):5.1f}%   "
              f"PNR {len(b1):2d}건 승률 {b1.win.mean()*100 if len(b1) else float('nan'):5.1f}%   N {int((g.cls=='N').sum()):2d}건")

    print("\n[8] 확장(ATR 1.0배) 판정 × 승패  — C 안의 전제")
    for lab, g in (("확장", d[d.extended]), ("비확장", d[~d.extended])):
        print(f"  {lab:4s} {len(g):3d}건  승률 {g.win.mean()*100:5.1f}%  손익 {g.pnl.sum():+13,.0f}  거래당 {g.pnl.mean():+10,.0f}  PF {pf(g.pnl):.2f}")
    e = d[d.extended]
    for c in ["N", "PR", "PNR"]:
        s2 = e[e.cls == c]
        if len(s2):
            print(f"    확장∩{c:3s} {len(s2):3d}건  승률 {s2.win.mean()*100:5.1f}%  손익 {s2.pnl.sum():+12,.0f}")
    if d.win.nunique() == 2:
        from sklearn.metrics import roc_auc_score
        ok = d.ext_atr.notna()
        print(f"  AUC(확장배수 높을수록 패) = {roc_auc_score(~d[ok].win, d[ok].ext_atr):.3f}  (판정불가 {int((~ok).sum())}건)")
    for m, g in d.groupby("month"):
        x = g[g.extended]
        print(f"    {m} 확장 {len(x):2d}건 승 {x.win.mean()*100 if len(x) else float('nan'):5.1f}% 손익 {x.pnl.sum():+11,.0f} / 비확장 {len(g)-len(x):3d}건 승 {g[~g.extended].win.mean()*100:5.1f}%")

    burst_report(d)

    print("\n[7] 큰 승자 상위 10 — 분류")
    for r in d.sort_values("pnl", ascending=False).head(10).itertuples():
        print(f"  {r.day} {r.entry[:5]} {r.sym} {r.cls:3s} {'EXT' if r.extended else '   '} {r.ext_atr:4.2f}ATR 눌림 {r.pb6_ref:+.2f}% reclaim {r.rc_min if r.rc_min==r.rc_min else '-'}분  MFE {r.mfe:+.2f}  {r.pnl:+,.0f}")


def burst_report(d):
    from scipy.stats import fisher_exact, mannwhitneyu
    d = d[d.n_dir.notna()].copy()
    d["burst"] = d.burst.astype(bool)
    print("\n" + "=" * 100 + "\n[B] BURST vs GRIND  (BURST = 직전 3봉 모두 원방향 AND 마지막 봉 몸통 최대)\n" + "=" * 100)
    print("  [B0] 연속지표 분포 (분위수 — 경계 탐색 아님)")
    qs = [0.1, 0.25, 0.5, 0.75, 0.9]
    for c in ["cum3", "body3", "rng3", "last_br", "pos3", "flag_prog"]:
        print(f"    {c:9s} " + "  ".join(f"q{int(q*100):02d} {d[c].quantile(q):+6.2f}" for q in qs))
    print("    원방향 봉 수 분포: " + "  ".join(f"{k}봉 {int((d.n_dir==k).sum())}" for k in range(4)))
    hist = np.histogram(d.cum3, bins=12)
    print("    cum3 히스토그램: " + " ".join(f"[{hist[1][i]:+.1f}]{hist[0][i]}" for i in range(12)))
    B, G = d[d.burst], d[~d.burst]
    print(f"\n  [B1] 그룹 크기  BURST {len(B)}  GRIND {len(G)}")
    print(f"  {'지표':12s} {'BURST 중앙':>10s} {'GRIND 중앙':>10s} {'MW p':>7s}")
    for c in ["mae1", "mae3", "mae6", "mae9", "mfe1", "mfe3", "mfe6", "mfe9", "mfe", "mae"]:
        p = mannwhitneyu(B[c], G[c]).pvalue if len(B) and len(G) else np.nan
        print(f"  {c:12s} {B[c].median():+10.2f} {G[c].median():+10.2f} {p:7.3f}")
    for lab, g in (("BURST", B), ("GRIND", G)):
        pb = g[g.cls != "N"]
        print(f"  {lab}: 승률 {g.win.mean()*100:5.1f}%  손익 {g.pnl.sum():+12,.0f}  거래당 {g.pnl.mean():+9,.0f}  PF {pf(g.pnl):.2f}"
              f"  | 6분 눌림 {len(pb)}/{len(g)}  6분 reclaim {int((pb.cls=='PR').sum())}/{len(pb)}"
              f"  보유중 언제든 재진행(reclaim) {int(pb.rc_any_min.notna().sum())}/{len(pb)}")
    if len(B) and len(G):
        t = [[int(B.win.sum()), int(len(B) - B.win.sum())], [int(G.win.sum()), int(len(G) - G.win.sum())]]
        print(f"  승률 Fisher p = {fisher_exact(t)[1]:.3f}   손익 MW p = {mannwhitneyu(B.pnl, G.pnl).pvalue:.3f}")
        pbB, pbG = B[B.cls != "N"], G[G.cls != "N"]
        if len(pbB) and len(pbG):
            t2 = [[int((pbB.cls=='PR').sum()), int((pbB.cls!='PR').sum())], [int((pbG.cls=='PR').sum()), int((pbG.cls!='PR').sum())]]
            print(f"  눌린 거래 중 6분 reclaim 비율 Fisher p = {fisher_exact(t2)[1]:.3f}")
    from sklearn.metrics import roc_auc_score
    if d.win.nunique() == 2:
        print("  [B2] 연속지표 단독 AUC (값 클수록 패배 예측; 0.5=무관)")
        for c in ["cum3", "body3", "last_br", "pos3", "flag_prog", "ext_atr"]:
            ok = d[c].notna()
            print(f"    {c:9s} AUC {roc_auc_score(~d[ok].win, d[ok][c]):.3f}   mae6 와 Spearman {d[c].corr(d.mae6, method='spearman'):+.3f}")
    print("  [B3] 월별 / regime 별")
    for key in ("month", "regime"):
        for m, g in d.groupby(key):
            b_, g_ = g[g.burst], g[~g.burst]
            print(f"    {str(m):8s} BURST {len(b_):2d}건 승 {b_.win.mean()*100 if len(b_) else float('nan'):5.1f}% 손익 {b_.pnl.sum():+11,.0f}"
                  f"  | GRIND {len(g_):3d}건 승 {g_.win.mean()*100 if len(g_) else float('nan'):5.1f}% 손익 {g_.pnl.sum():+12,.0f}")


if __name__ == "__main__":
    main()
