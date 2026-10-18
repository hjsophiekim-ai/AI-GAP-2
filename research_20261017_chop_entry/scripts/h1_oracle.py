"""최근 구간 — '그날 애초에 먹을 게 있었나' (2026-10-18). READ-ONLY.

질문: 최근 10일 손실이 (a) 진입 선택이 나빴던 것인지 (b) 그날은 뭘 잡아도 손해였는지.

방법: 그날 뜬 **확정플래그 전부**(:TW2_3SLOT_CONFIRM, 체결/차단 무관)를 대상으로
  "그 방향 ETF 를 결정시각 1분봉 종가에 사서, 다음 반대방향 확정 또는 15:00 종가에 판"
  사후수익 %를 계산한다 (flag_select.csv 와 같은 정의).
  · 실제 체결한 것 vs 막힌 것
  · 그날 최고의 플래그 하나만 골랐다면 (oracle)
  · 하이닉스 일중 변동폭 — 애초에 움직임이 있었나
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rep2  # noqa: E402

BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
_B = {}


def bars(day, tag):
    k = (day, tag)
    if k not in _B:
        p = f"{BARS}/replay_{day}_{tag}_1m.csv"
        if not os.path.exists(p):
            _B[k] = None
        else:
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            _B[k] = x.drop_duplicates("datetime", keep="last").set_index("datetime").sort_index()
    return _B[k]


def flags_of(day, tag="CHOP"):
    p = f"{rep2.LED}/{tag}_{day}.json"
    if not os.path.exists(p):
        return None
    o = json.load(open(p, encoding="utf-8"))
    rows = []
    for s in o.get("sig", []):
        sid = str(s.get("signal_id") or "")
        if not sid.endswith(":TW2_3SLOT_CONFIRM") or not sid.startswith(day):
            continue
        t = pd.Timestamp(str(s["detected_at"])[:19])
        if not (pd.Timestamp("09:00").time() <= t.time() < pd.Timestamp("15:00").time()):
            continue
        rows.append(dict(day=day, t=t, d=1 if s.get("direction") == "UP_RED" else -1,
                         result=s.get("order_result"), why=s.get("block_reason") or ""))
    if not rows:
        return pd.DataFrame()
    f = pd.DataFrame(rows).drop_duplicates(["t", "d"]).sort_values("t").reset_index(drop=True)
    lg, iv = bars(day, "long"), bars(day, "inverse")
    end = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:00")
    out = []
    for r in f.itertuples():
        etf = lg if r.d == 1 else iv
        if etf is None:
            continue
        nxt = f[(f.t > r.t) & (f.d == -r.d)]
        t_out = min(nxt.t.iloc[0], end) if len(nxt) else end
        pi = etf["close"][etf.index <= r.t]
        po = etf["close"][etf.index <= t_out]
        if pi.empty or po.empty:
            continue
        out.append(dict(day=day, t=r.t, dir=("UP" if r.d == 1 else "DN"), result=r.result,
                        why=r.why, ret=(po.iloc[-1] / pi.iloc[-1] - 1) * 100,
                        hold=(t_out - r.t).total_seconds() / 60))
    return pd.DataFrame(out)


def main():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    r10, r16 = days[-10:], days[-16:]
    F = pd.concat([x for x in (flags_of(d) for d in r16) if x is not None and len(x)],
                  ignore_index=True)
    # 실제 손익
    pnl = {}
    for d in r16:
        p = f"{rep2.LED}/CHOP_{d}.json"
        pnl[d] = (sum(t["krw"] for t in rep2.trades(json.load(open(p, encoding="utf-8"))))
                  if os.path.exists(p) else 0.0)

    print("확정플래그 = 그날 시스템이 '진입 후보'로 본 것 전부 (체결/차단 무관)")
    print("사후수익 = 그 방향 ETF 를 결정시각에 사서 다음 반대확정/15:00 에 판 %\n")
    print(f"  {'일자':10s} {'플래그':>4s} {'양수':>4s} {'최고':>7s} {'최저':>7s} {'평균':>7s} "
          f"{'체결':>3s} {'체결평균':>8s} {'막힌것평균':>9s} {'하이닉스폭':>9s} {'실제손익':>11s}")
    for d in r16:
        g = F[F.day == d]
        hy = bars(d, "hynix")
        rng = np.nan
        if hy is not None:
            s = hy[(hy.index.time >= pd.Timestamp("09:00").time()) & (hy.index.time <= pd.Timestamp("15:00").time())]
            if len(s):
                rng = (s["high"].max() / s["low"].min() - 1) * 100
        ex = g[g.result == "EXECUTED"]
        bl = g[g.result != "EXECUTED"]
        mark = " " if d not in r10 else "*"
        print(f" {mark}{d:9s} {len(g):4d} {int((g.ret > 0).sum()) if len(g) else 0:4d} "
              f"{g.ret.max() if len(g) else np.nan:+7.2f} {g.ret.min() if len(g) else np.nan:+7.2f} "
              f"{g.ret.mean() if len(g) else np.nan:+7.2f} {len(ex):3d} "
              f"{ex.ret.mean() if len(ex) else np.nan:+8.2f} {bl.ret.mean() if len(bl) else np.nan:+9.2f} "
              f"{rng:9.2f} {pnl[d]:+11,.0f}")

    for lab, sub in (("최근 10일", r10), ("최근 16일", r16)):
        g = F[F.day.isin(sub)]
        ex, bl = g[g.result == "EXECUTED"], g[g.result != "EXECUTED"]
        nz = [d for d in sub if len(F[F.day == d])]
        best = sum(F[F.day == d].ret.max() for d in nz)
        pos = [d for d in nz if (F[F.day == d].ret > 0).any()]
        print(f"\n[{lab}]  플래그 {len(g)}건 / {len(nz)}일")
        print(f"  전체       평균 {g.ret.mean():+.2f}%  승률 {(g.ret > 0).mean() * 100:.0f}%  "
              f"최고 {g.ret.max():+.2f}  최저 {g.ret.min():+.2f}")
        print(f"  실제 체결  {len(ex):2d}건 평균 {ex.ret.mean():+.2f}%  승률 {(ex.ret > 0).mean() * 100:.0f}%")
        print(f"  막힌 것    {len(bl):2d}건 평균 {bl.ret.mean():+.2f}%  승률 {(bl.ret > 0).mean() * 100:.0f}%")
        print(f"  **양수 플래그가 하나라도 있던 날: {len(pos)}/{len(nz)}일**")
        print(f"  oracle(매일 그날 최고 플래그 1개만): 합 {best:+.1f}%p, 일평균 {best / max(len(nz), 1):+.2f}%p")

    print("\n[최근 10일 — 그날 최고 플래그와 실제로 잡은 것]")
    for d in r10:
        g = F[F.day == d]
        if not len(g):
            print(f"  {d}  플래그 없음  (실제손익 {pnl[d]:+,.0f})")
            continue
        b = g.loc[g.ret.idxmax()]
        ex = g[g.result == "EXECUTED"]
        got = ", ".join(f"{r.t:%H:%M}{r.dir} {r.ret:+.2f}" for r in ex.itertuples()) or "없음"
        print(f"  {d}  최고 {b.t:%H:%M}{b.dir} {b.ret:+6.2f}%  |  잡은 것: {got}  |  실제 {pnl[d]:+,.0f}")


if __name__ == "__main__":
    main()
