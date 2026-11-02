"""흔들림 견디기 — TREND 래더 손절 거래 반사실 (2026-11-02).

입력: research_20261030_tick_arm3/out/trades_N1.csv (variant A, 11영업일 10/16~10/30 재생)
      research_20261020_h50_hold/data/replay_<day>_{long,inverse,hynix}_1m.csv
대상: exits == TIME_WINDOW_STOP_LOSS (오늘 11/02 11:27 과 같은 경로)
변형은 사전 고정(스윕하지 않음). 손절을 피한 뒤의 청산은 모든 변형이 공통 규칙:
  회복 청산(net >= -0.2%, production CHOP_STOP_RECOVER_PCT) / 최대 20분 / 하드 바닥 / 15:00
→ 회복 후 추가 상승은 0으로 잡는다(변형에 불리한 보수적 가정).
"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
TR = ROOT / "research_20261030_tick_arm3/out/trades_N1.csv"
BARS = ROOT / "research_20261020_h50_hold/data"
FEE = 0.000036
STOP = -1.3          # TREND(N1) 래더 손절, 완성 3분봉 종가 기준
RECOVER = -0.2
WAIT_MIN = 20
FLOOR = -3.0
WIDE = -2.0
EARLY_MIN = 10
BOX_MIN, BOX_SKIP = 30, 3
SESSION_END = "15:00"


def load(day, kind):
    d = pd.read_csv(BARS / f"replay_{day}_{kind}_1m.csv", parse_dates=["datetime"])
    return d.set_index("datetime").sort_index()


def net(px, e):
    return (px / e - 1) * 100 - FEE * 100 * (1 + px / e)


def pnl(q, e, x):
    return q * (x - e) - FEE * q * (e + x)


def completed_1m(b, t):
    return b[b.index + pd.Timedelta(minutes=1) <= t]


def bars3(b):
    return b.resample("3min", origin="start_day", offset="0min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def box_broken(hy, t, up):
    w = completed_1m(hy, t)
    w = w[w.index.time >= pd.Timestamp("09:00").time()]
    if len(w) < BOX_MIN:
        return True                       # 판정불가 -> 손절 (production fail-safe)
    tail = w.iloc[-BOX_MIN:]
    box = tail.iloc[:-BOX_SKIP]
    last = float(w["close"].iloc[-1])
    return last < box["low"].min() if up else last > box["high"].max()


def follow(etf, hy, e, t0, *, up, stop3=None, box=False, floor=FLOOR, wait=WAIT_MIN):
    """t0 부터 1분봉을 따라가며 공통 청산. 반환 (청산시각, 가격, 사유)."""
    end = min(t0 + pd.Timedelta(minutes=wait),
              pd.Timestamp(f"{t0.date()} {SESSION_END}"))
    seg = etf[(etf.index >= t0) & (etf.index < end)]
    for ts, r in seg.iterrows():
        tc = ts + pd.Timedelta(minutes=1)
        c = float(r.close)
        if net(c, e) >= RECOVER:
            return tc, c, "RECOVER"
        if net(c, e) <= floor:
            return tc, c, "FLOOR"
        if stop3 is not None and tc.minute % 3 == 0 and net(c, e) <= stop3:
            return tc, c, "STOP3"
        if box and net(c, e) <= STOP and box_broken(hy, tc, up):
            return tc, c, "BOX_BREAK"
    last = etf[etf.index < end].iloc[-1]
    return end, float(last.close), "TIMEOUT"


def main():
    t = pd.read_csv(TR, encoding="utf-8-sig")
    t = t[(t.variant == "A") & (t.exits == "TIME_WINDOW_STOP_LOSS")].copy()
    rows = []
    for _, r in t.iterrows():
        day = str(r.day)
        kind = "long" if r.sym == "0193T0" else "inverse"
        etf, hy = load(day, kind), load(day, "hynix")
        up = kind == "long"
        e, q = float(r.entry_px), int(r.qty)
        te = pd.Timestamp(r.entry_t).tz_localize(None)
        tx = pd.Timestamp(r.exit_t).tz_localize(None)
        A = float(r.pnl)
        res = {"day": day, "entry": te.strftime("%H:%M"), "stop": tx.strftime("%H:%M"),
               "sym": "레버" if up else "인버스", "held_min": (tx - te).seconds // 60,
               "A": A, "A_net": net(float(r.exit_px), e)}
        # 손절 후 20분 경로 (참고)
        seg = etf[(etf.index >= tx) & (etf.index < tx + pd.Timedelta(minutes=20))]
        res["post20_max"] = net(float(seg.high.max()), e)
        res["post20_min"] = net(float(seg.low.min()), e)
        # V1 박스유예(CHOP 구조손절을 TREND 에 그대로)
        if box_broken(hy, tx, up):
            res["V1"], res["V1_why"] = A, "BOX_EXEC"
        else:
            ts, px, why = follow(etf, hy, e, tx, up=up, box=True)
            res["V1"], res["V1_why"] = pnl(q, e, px), why
        # V2 3분봉 2연속 확인: 다음 완성 3분봉 종가가 여전히 STOP 이하면 손절
        nb = bars3(etf)
        nxt = nb[nb.index >= tx]
        if len(nxt):
            c2 = float(nxt.iloc[0].close)
            if net(c2, e) <= STOP:
                res["V2"], res["V2_why"] = pnl(q, e, c2), "CONFIRMED"
            else:
                ts, px, why = follow(etf, hy, e, tx + pd.Timedelta(minutes=3), up=up, stop3=STOP)
                res["V2"], res["V2_why"] = pnl(q, e, px), "SURVIVE→" + why
        # V3 손절 -2.0% 로 완화 (3분봉 종가)
        ts, px, why = follow(etf, hy, e, tx, up=up, stop3=WIDE, floor=-99, wait=60)
        res["V3"], res["V3_why"] = pnl(q, e, px), why
        # V4 진입 후 10분 이내만 -2.0%, 이후 원래 -1.3
        if (tx - te).seconds / 60 > EARLY_MIN:
            res["V4"], res["V4_why"] = A, "LATE=A"
        else:
            # 10분 경과 전까지 -2.0, 이후 -1.3
            ts, px, why = follow(etf, hy, e, tx, up=up, stop3=WIDE, floor=-99,
                                 wait=max(1, EARLY_MIN - (tx - te).seconds // 60))
            if why == "TIMEOUT":
                ts2, px, why2 = follow(etf, hy, e, ts, up=up, stop3=STOP, floor=-99, wait=60)
                why = "→" + why2
            res["V4"], res["V4_why"] = pnl(q, e, px), why
        # V5 절반만 손절 + 절반 V1
        res["V5"] = 0.5 * A + 0.5 * res["V1"]
        rows.append(res)
    d = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(d.round(2).to_string(index=False))
    print()
    for v in ["V1", "V2", "V3", "V4", "V5"]:
        diff = d[v] - d["A"]
        print(f"{v}: Δ합 {diff.sum():+,.0f}  개선 {int((diff>0).sum())} / 악화 {int((diff<0).sum())} / 동일 {int((diff==0).sum())}"
              f"  최악Δ {diff.min():+,.0f}  최대Δ {diff.max():+,.0f}  상위1제외 {diff.sum()-diff.max():+,.0f}")
    print(f"A 손절 합계 {d.A.sum():+,.0f}  ({len(d)}건)")
    d.to_csv(Path(__file__).resolve().parents[1] / "out_cf.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
