"""PRE-FLAG BURST 진단 (2026-11-02, research only, production 무수정).

d1_diag(T+3 직전 3봉 기준)과 **별개**다. 1분봉 로딩/포지션 묶기만 재사용한다.

사전 고정 정의 (결과를 보고 바꾸지 않는다):
  플래그봉      = sid 의 HHMMSS 에 시작하는 3분봉. T+3 = 플래그봉 시작 + 6분.
  PRE-FLAG 3봉  = 플래그봉 **바로 앞** 완성 3분봉 3개 (당일봉만, 플래그봉 제외).
                  당일 3개 미만(09:00~09:06 플래그)이면 판정불가(NA).
  방향          = 보유 ETF 상승방향 (RED=레버리지 상승, BLUE=인버스 상승 = 하이닉스 하락, 대칭)
  장대봉        = 몸통 >= 그 봉 직전까지의 ATR14(완성 3분봉, 전일 포함) x 1.0
  PRE-FLAG BURST = 3봉 모두 원방향 AND 장대봉 1개 이상.  나머지 = 일반(완만/혼합)
  손절          = 청산사유에 STOP_LOSS 또는 B3_SL
  손절 후 회복  = 손절 청산 후 30분 안에 1분봉 종가 >= 실제 진입가
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import d1_diag as D  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
FEE = D.FEE


def bars3(b1):
    agg = b1.resample("3min", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return agg


def atr_before(b3, t, n=14):
    x = b3[b3.index + pd.Timedelta(minutes=3) <= t]
    if len(x) < 5:
        return np.nan
    pc = x["close"].shift(1)
    tr = pd.concat([x.high - x.low, (x.high - pc).abs(), (x.low - pc).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean())


def analyse(day, ep, b):
    buy = ep["buys"][0]
    te = pd.Timestamp(buy["t"][:19])
    q, e = buy["qty"], float(buy["px"])
    sells = ep["sells"]
    if not sells:
        return None
    tx = pd.Timestamp(sells[-1]["t"][:19])
    pnl = sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e) for s in sells)
    reason = ";".join(sorted({a.split(":")[0] for s in sells for a in s.get("acts", [])}))
    sid = ep["ent"].get("sid", "")
    flag = pd.Timestamp(f"{sid[:8]} {sid[9:11]}:{sid[11:13]}:{sid[13:15]}")
    t3 = flag + pd.Timedelta(minutes=6)
    b3 = bars3(b)
    fb = b3.loc[flag] if flag in b3.index else None
    pre = b3[(b3.index < flag) & (b3.index.normalize() == flag.normalize())].iloc[-3:]
    r = dict(day=day, month=day[:6], sym="L" if ep["sym"] == D.LONG else "I",
             flag=flag.strftime("%H:%M"), t3=t3.strftime("%H:%M"), entry=te.strftime("%H:%M:%S"),
             e_brk=":E_BRK" in sid, regime=ep["ent"].get("regime"), entry_px=e, qty=q,
             exit=tx.strftime("%H:%M:%S"), reason=reason, pnl=pnl, win=pnl > 0,
             stop=("STOP_LOSS" in reason) or ("B3_SL" in reason))
    if fb is not None:
        r["flag_close"] = float(fb["close"])
        r["flag_body"] = (float(fb["close"]) / float(fb["open"]) - 1) * 100
        r["prog_flag_to_entry"] = (e / float(fb["close"]) - 1) * 100
    if len(pre) == 3:
        body = (pre["close"] - pre["open"]) / pre["open"] * 100
        rng = (pre["high"] - pre["low"]) / pre["open"] * 100
        big = []
        for ts, row in pre.iterrows():
            a = atr_before(b3, ts)
            big.append(bool(a == a and (row.close - row.open) >= a))
        hi, lo = float(pre.high.max()), float(pre.low.min())
        r.update(pre_ok=True,
                 b1=body.iloc[0], b2=body.iloc[1], b3=body.iloc[2],
                 r1=rng.iloc[0], r2=rng.iloc[1], r3=rng.iloc[2],
                 n_dir=int((body > 0).sum()),
                 cum3=(float(pre.close.iloc[-1]) / float(pre.open.iloc[0]) - 1) * 100,
                 n_big=int(sum(big)),
                 pos_entry=(e - lo) / (hi - lo) if hi > lo else np.nan)
        r["burst"] = bool(r["n_dir"] == 3 and r["n_big"] >= 1)
    else:
        r.update(pre_ok=False, burst=False)
    # ── 단기고점 진입 (정의 무관 연속지표) + 눌림목 지정가 반사실 — 사전등록 ──
    #   hi30  = (ref - low30) / (high30 - low30), 30분 = production CHOP_BOX_MINUTES
    #   눌림 지정가 = ref - 0.5 x ATR14(3분봉), T+3 이후 9분 안 1분봉 low 도달 시 체결, 아니면 포기
    pre1 = b[b.index + pd.Timedelta(minutes=1) <= t3]
    ref = float(pre1.close.iloc[-1])
    w30 = pre1[pre1.index >= t3 - pd.Timedelta(minutes=30)]
    h30, l30 = float(w30.high.max()), float(w30.low.min())
    r["ref"] = ref
    r["hi30"] = (ref - l30) / (h30 - l30) if h30 > l30 else np.nan
    # 고점 판정 (사전등록): ref >= production CHOP 박스 상단 (30분 중 마지막 3분 제외, 1분봉 high)
    box = pre1[(pre1.index >= t3 - pd.Timedelta(minutes=30)) & (pre1.index < t3 - pd.Timedelta(minutes=3))]
    r["box_hi"] = float(box.high.max()) if len(box) else np.nan
    r["at_high"] = bool(len(box) and ref >= float(box.high.max()))
    a14 = atr_before(b3, t3)
    r["atr_pct"] = a14 / ref * 100 if a14 == a14 else np.nan
    r["pb_fill"], r["pnl_pb"] = False, 0.0
    if a14 == a14:
        lim = float(np.floor((ref - 0.5 * a14) / 5) * 5)       # 호가 5원 단위 내림
        win9 = b[(b.index >= t3) & (b.index < t3 + pd.Timedelta(minutes=9))]
        hit = win9[win9.low <= lim]
        r["pb_lim"] = lim
        # 근사: 청산은 실제 그대로(시각·가격), 진입가만 지정가. 체결 전에 이미 청산됐으면 미체결.
        if len(hit) and hit.index[0] + pd.Timedelta(minutes=1) < tx:
            r["pb_fill"] = True
            r["pb_fill_t"] = (hit.index[0] + pd.Timedelta(minutes=1)).strftime("%H:%M")
            r["pnl_pb"] = sum(sl["qty"] * (sl["px"] - lim) - FEE * sl["qty"] * (sl["px"] + lim) for sl in sells)
    after = b[b.index >= te.floor("min")]
    for k in (1, 3, 6, 9):
        seg = after.iloc[:k]
        r[f"mae{k}"] = (float(seg.low.min()) / e - 1) * 100
        r[f"mfe{k}"] = (float(seg.high.max()) / e - 1) * 100
    hold = b[(b.index >= te.floor("min")) & (b.index < tx)]
    hold = hold if len(hold) else after.iloc[:1]
    r["mfe"] = (float(hold.high.max()) / e - 1) * 100
    r["mae"] = (float(hold.low.min()) / e - 1) * 100
    if r["stop"]:
        post = b[(b.index >= tx.floor("min")) & (b.index < tx + pd.Timedelta(minutes=30))]
        hit = post[post.close >= e]
        r["recover30"] = bool(len(hit))
        r["recover_min"] = (hit.index[0] + pd.Timedelta(minutes=1) - tx).total_seconds() / 60 if len(hit) else np.nan
        r["post30_mfe"] = (float(post.high.max()) / e - 1) * 100 if len(post) else np.nan
    return r


def pf(x):
    g, l = x[x > 0].sum(), -x[x < 0].sum()
    return g / l if l else np.inf


def report(d):
    from scipy.stats import fisher_exact, mannwhitneyu
    print(f"전체 {len(d)}거래  총손익 {d.pnl.sum():+,.0f}  PF {pf(d.pnl):.3f}  승률 {d.win.mean()*100:.1f}%")
    na = d[~d.pre_ok]
    print(f"판정불가(당일 선행 3봉 부족) {len(na)}건  손익 {na.pnl.sum():+,.0f}  승률 {na.win.mean()*100 if len(na) else float('nan'):.1f}%")
    v = d[d.pre_ok].copy()
    print(f"\n[0] 분포 — 판정가능 {len(v)}건 (분위수, 경계 탐색 아님)")
    qs = [0.1, 0.25, 0.5, 0.75, 0.9]
    for c in ["cum3", "b1", "b2", "b3", "r3", "flag_body", "prog_flag_to_entry", "pos_entry"]:
        print(f"  {c:20s} " + "  ".join(f"q{int(q*100):02d} {v[c].quantile(q):+6.2f}" for q in qs))
    print("  원방향 봉 수: " + "  ".join(f"{k}봉 {int((v.n_dir==k).sum())}" for k in range(4))
          + "   | 장대봉 수: " + "  ".join(f"{k}개 {int((v.n_big==k).sum())}" for k in range(4)))
    h = np.histogram(v.cum3, bins=10)
    print("  cum3 히스토그램: " + " ".join(f"[{h[1][i]:+.2f}]{h[0][i]}" for i in range(10)))
    print("  원방향 봉 수별 성과 (경계가 자연스러운지 보기 위한 표)")
    for k in range(4):
        g = v[v.n_dir == k]
        if len(g):
            print(f"    {k}봉 {len(g):3d}건 승률 {g.win.mean()*100:5.1f}% PF {pf(g.pnl):5.2f} 손익 {g.pnl.sum():+12,.0f}  mae6 중앙 {g.mae6.median():+.2f}")
    B, G = v[v.burst], v[~v.burst]
    print(f"\n[1] PRE-FLAG BURST {len(B)}건 vs 일반 {len(G)}건 — 진입 직후 눌림")
    print(f"  {'':8s} {'BURST 중앙':>10s} {'일반 중앙':>10s} {'MW p':>7s}")
    for c in ["mae1", "mae3", "mae6", "mae9", "mfe1", "mfe3", "mfe6", "mfe9", "mfe", "mae", "prog_flag_to_entry", "pos_entry"]:
        p = mannwhitneyu(B[c].dropna(), G[c].dropna()).pvalue if len(B) > 1 and len(G) > 1 else np.nan
        print(f"  {c:8s} {B[c].median():+10.2f} {G[c].median():+10.2f} {p:7.3f}")
    print("\n[2] 손절 후 30분 내 진입가 회복")
    for lab, g in (("BURST", B), ("일반", G), ("판정불가", na)):
        s = g[g.stop]
        print(f"  {lab:6s} 손절 {len(s):2d}건  회복 {int(s.recover30.sum()) if len(s) else 0}/{len(s)}"
              f"  ({s.recover30.mean()*100 if len(s) else float('nan'):.0f}%)  회복까지 중앙 {s.recover_min.median() if len(s) else float('nan'):.0f}분")
    sB, sG = B[B.stop], G[G.stop]
    if len(sB) and len(sG):
        t = [[int(sB.recover30.sum()), int((~sB.recover30.astype(bool)).sum())], [int(sG.recover30.sum()), int((~sG.recover30.astype(bool)).sum())]]
        print(f"  회복비율 Fisher p = {fisher_exact(t)[1]:.3f}")
    print("\n[3] 최종 성과")
    for lab, g in (("BURST", B), ("일반", G)):
        print(f"  {lab:6s} {len(g):3d}건 승률 {g.win.mean()*100 if len(g) else float('nan'):5.1f}% PF {pf(g.pnl):5.2f} 손익 {g.pnl.sum():+12,.0f} 거래당 {g.pnl.mean() if len(g) else 0:+9,.0f}  손절 {int(g.stop.sum())}건")
    if len(B) and len(G):
        t = [[int(B.win.sum()), int(len(B) - B.win.sum())], [int(G.win.sum()), int(len(G) - G.win.sum())]]
        rng = np.random.default_rng(0)
        bs = [rng.choice(B.win.to_numpy(), len(B)).mean() - rng.choice(G.win.to_numpy(), len(G)).mean() for _ in range(5000)]
        print(f"  승률차 {(B.win.mean()-G.win.mean())*100:+.1f}%p 95%CI [{np.percentile(bs,2.5)*100:+.1f}, {np.percentile(bs,97.5)*100:+.1f}]"
              f"  Fisher p {fisher_exact(t)[1]:.3f}  손익 MW p {mannwhitneyu(B.pnl, G.pnl).pvalue:.3f}")
    from sklearn.metrics import roc_auc_score
    if v.win.nunique() == 2:
        print("  연속지표 단독 AUC(클수록 패 예측, 0.5=무관): " + "  ".join(
            f"{c} {roc_auc_score(~v.win, v[c].fillna(v[c].median())):.3f}" for c in ["cum3", "b3", "prog_flag_to_entry", "pos_entry"]))
    print("\n[4] 월별 / regime 별")
    for key in ("month", "regime"):
        for m, g in v.groupby(key):
            b_, g_ = g[g.burst], g[~g.burst]
            print(f"  {str(m):7s} BURST {len(b_):2d}건 승 {b_.win.mean()*100 if len(b_) else float('nan'):5.1f}% 손익 {b_.pnl.sum():+11,.0f}"
                  f" | 일반 {len(g_):3d}건 승 {g_.win.mean()*100 if len(g_) else float('nan'):5.1f}% 손익 {g_.pnl.sum():+12,.0f}")
    hi30_report(d)
    hybrid_report(d)
    print("\n[5] BURST 전수")
    for r in B.sort_values(["day", "entry"]).itertuples():
        print(f"  {r.day} 플래그 {r.flag} 진입 {r.entry[:5]} {r.sym} {r.regime:5s} 3봉 {r.b1:+.2f}/{r.b2:+.2f}/{r.b3:+.2f} 장대{r.n_big}"
              f" 추가진행 {r.prog_flag_to_entry:+.2f}% 위치 {r.pos_entry:.2f}  mae6 {r.mae6:+.2f} mfe {r.mfe:+.2f}  {r.reason[:28]:28s} {r.pnl:+,.0f}"
              + (f"  손절후회복 {'O' if r.recover30 else 'X'}" if r.stop else ""))


def hi30_report(d):
    from sklearn.metrics import roc_auc_score
    d = d[d.hi30.notna()].copy()
    print("\n\n" + "=" * 100 + "\n[H] 단기고점 진입(hi30 = 30분 범위 내 T+3 가격 위치) — 정의 무관 연속지표\n" + "=" * 100)
    print("  분포: " + "  ".join(f"q{int(q*100):02d} {d.hi30.quantile(q):.2f}" for q in (0.1, 0.25, 0.5, 0.75, 0.9))
          + f"   30분 신고가(>=0.999) 비율 {(d.hi30 >= 0.999).mean()*100:.0f}%")
    d["q"] = pd.qcut(d.hi30.rank(method="first"), 5, labels=["Q1(저)", "Q2", "Q3", "Q4", "Q5(고)"])
    print(f"  {'구간':7s} {'건':>3s} {'hi30범위':>11s} {'승률':>6s} {'PF':>5s} {'손익':>12s} {'mae6중앙':>8s} {'손절':>4s} {'손절후회복':>8s}")
    for k, g in d.groupby("q", observed=True):
        st = g[g.stop]
        print(f"  {k:7s} {len(g):3d} {g.hi30.min():.2f}~{g.hi30.max():.2f} {g.win.mean()*100:5.1f}% {pf(g.pnl):5.2f} {g.pnl.sum():+12,.0f} {g.mae6.median():+8.2f} {len(st):4d} "
              f"{int(st.recover30.sum()) if len(st) else 0}/{len(st)}")
    if d.win.nunique() == 2:
        print(f"  AUC(hi30 높을수록 패) {roc_auc_score(~d.win, d.hi30):.3f}   hi30~mae6 Spearman {d.hi30.corr(d.mae6, method='spearman'):+.3f}")
    print("  월별 AUC: " + "  ".join(f"{m[4:]} {roc_auc_score(~g.win, g.hi30):.2f}(n={len(g)})" for m, g in d.groupby('month') if g.win.nunique() == 2))
    print("\n  [눌림목 지정가 반사실 — 근사, 재생 아님] 지정가 = ref − 0.5×ATR14, 9분 유효, 미체결=포기")
    print(f"  ATR14 중앙 {d.atr_pct.median():.2f}% → 지정가 깊이 중앙 {(d.atr_pct*0.5).median():.2f}%")
    for lab, g in (("전체", d), ("Q5(고)", d[d.q == "Q5(고)"]), ("Q4+Q5", d[d.q.isin(["Q4", "Q5(고)"])]), ("Q1~Q3", d[d.q.isin(["Q1(저)", "Q2", "Q3"])])):
        f = g[g.pb_fill]; nf = g[~g.pb_fill]
        print(f"  {lab:6s} {len(g):3d}건 체결 {len(f):3d}  현행 {g.pnl.sum():+12,.0f} → 눌림 {g.pnl_pb.sum():+12,.0f}  Δ {g.pnl_pb.sum()-g.pnl.sum():+11,.0f}"
              f" | 놓친 승자 {nf[nf.win].pnl.sum():+11,.0f}({int(nf.win.sum())}건)  피한 손실 {nf[~nf.win].pnl.sum():+11,.0f}({int((~nf.win).sum())}건)"
              f"  체결분 개선 {(f.pnl_pb - f.pnl).sum():+10,.0f}")


def _mdd(daily):
    c = daily.cumsum()
    return float((c.cummax() - c).max())


def hybrid_report(d):
    d = d.copy()
    d["pnl_h1"] = np.where(d.at_high, d.pnl_pb, d.pnl)
    d["pnl_h2"] = np.where(d.at_high, 0.0, d.pnl)
    print("\n" + "=" * 100 + "\n[X] 하이브리드 — 고점(T+3 기준가 >= 30분 박스 상단)만 눌림대기/포기, 나머지 즉시 (근사, 재생 아님)\n" + "=" * 100)
    H, N = d[d.at_high], d[~d.at_high]
    for lab, g in (("고점", H), ("비고점", N)):
        st = g[g.stop]
        print(f"  {lab:4s} {len(g):3d}건  승률 {g.win.mean()*100 if len(g) else float('nan'):5.1f}%  PF {pf(g.pnl):5.2f}  손익 {g.pnl.sum():+12,.0f}  거래당 {g.pnl.mean() if len(g) else 0:+9,.0f}"
              f"  mae6 중앙 {g.mae6.median():+.2f}  손절 {len(st)}건(30분내 회복 {int(st.recover30.sum()) if len(st) else 0})")
    if len(H) and len(N):
        from scipy.stats import fisher_exact, mannwhitneyu
        t = [[int(H.win.sum()), int(len(H) - H.win.sum())], [int(N.win.sum()), int(len(N) - N.win.sum())]]
        print(f"  승률 Fisher p {fisher_exact(t)[1]:.3f}   손익 MW p {mannwhitneyu(H.pnl, N.pnl).pvalue:.3f}   mae6 MW p {mannwhitneyu(H.mae6, N.mae6).pvalue:.3f}")
    print(f"  {'':4s} {'총손익':>12s} {'PF':>5s} {'MDD':>10s} {'최악일':>10s} {'상위1제외':>12s} {'상위2제외':>12s} {'거래':>4s}")
    for v in ("pnl", "pnl_h1", "pnl_h2"):
        x = d[v]
        traded = d[(x != 0) | (v == "pnl")]
        daily = d.groupby("day")[v].sum()
        top = x.sort_values(ascending=False)
        lab = {"pnl": "A", "pnl_h1": "H1", "pnl_h2": "H2"}[v]
        print(f"  {lab:4s} {x.sum():+12,.0f} {pf(x):5.2f} {_mdd(daily):10,.0f} {daily.min():+10,.0f} {x.sum()-top.iloc[0]:+12,.0f} {x.sum()-top.iloc[:2].sum():+12,.0f} {len(traded):4d}")
    hf = H[H.pb_fill]; hn = H[~H.pb_fill]
    print(f"  H1 고점 {len(H)}건 중 눌림 체결 {len(hf)} — 평균 진입가 개선 {((hf.ref - hf.pb_lim) / hf.ref * 100).mean() if len(hf) else float('nan'):.2f}%"
          f"  체결분 개선 {(hf.pnl_pb - hf.pnl).sum():+,.0f}  | 미체결 {len(hn)}: 놓친 승자 {hn[hn.win].pnl.sum():+,.0f}({int(hn.win.sum())}건) 피한 손실 {hn[~hn.win].pnl.sum():+,.0f}({int((~hn.win).sum())}건)")
    print(f"  H2 포기: 놓친 승자 {H[H.win].pnl.sum():+,.0f}({int(H.win.sum())}건)  피한 손실 {H[~H.win].pnl.sum():+,.0f}({int((~H.win).sum())}건)")
    print("  월별 / regime 별 Δ(H1−A, H2−A)")
    for key in ("month", "regime"):
        for m, g in d.groupby(key):
            print(f"    {str(m):7s} 거래 {len(g):3d} 고점 {int(g.at_high.sum()):2d}  A {g.pnl.sum():+12,.0f}  H1Δ {g.pnl_h1.sum()-g.pnl.sum():+11,.0f}  H2Δ {g.pnl_h2.sum()-g.pnl.sum():+11,.0f}")
    print("  고점 거래 전수")
    for r in H.sort_values(["day", "entry"]).itertuples():
        print(f"    {r.day} {r.entry[:5]} {r.sym} {str(r.regime):5s} ref {r.ref:,.0f} 박스상단 {r.box_hi:,.0f} hi30 {r.hi30:.2f}  {r.reason[:26]:26s} A {r.pnl:+10,.0f}"
              f"  H1 {'체결 ' + str(r.pb_fill_t) if r.pb_fill else '포기':10s} {r.pnl_pb:+10,.0f}  mfe {r.mfe:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="L85_D")
    ap.add_argument("--csv", default=str(Path(__file__).resolve().parents[1] / "out" / "preflag_trades.csv"))
    a = ap.parse_args()
    rows = []
    files = [f for f in sorted(D.OUT.glob(f"{a.tag}_2026*.json")) if f.stem.split("_")[-1] >= D.REAL_FROM]
    for f in files:
        day = f.stem.split("_")[-1]
        js = json.loads(f.read_text(encoding="utf-8"))
        cache = {}
        for ep in D.episodes(js, day):
            cache.setdefault(ep["sym"], D.bars(day, ep["sym"]))
            r = analyse(day, ep, cache[ep["sym"]])
            if r:
                rows.append(r)
    d = pd.DataFrame(rows)
    Path(a.csv).parent.mkdir(parents=True, exist_ok=True)
    d.to_csv(a.csv, index=False, encoding="utf-8-sig")
    print(f"[{a.tag}] {len(files)}일 ({files[0].stem[-8:]}~{files[-1].stem[-8:]})")
    report(d)


if __name__ == "__main__":
    main()
