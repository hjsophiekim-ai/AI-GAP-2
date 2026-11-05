"""MU 플래그 = HY 추세 재가속 신호? — 1회 진단 (재생 없음, 날짜별 순차, CSV 1장).

MU 플래그 : MU 3분봉 MACD(12,26,9) 확정 교차 (production mu_macd 와 같은 규칙), MU 1분봉 = production mu_assist.load_mu_1m.
HY 플래그 : production signal_engine (프리마켓 포함, 08:50~08:59 패딩 제외, 전 2일 seed).
A 조건   : (2) MU 시각 기준 90분 이내 같은 방향 HY 플래그 (3) 그 뒤 반대 HY 플래그 없음
           (4) HY 체결강도 방향일치(직전 60초 평균, 100 기준) ∧ HY gap 같은방향 유지(g>0) ∧ hy_gap_state ∈ {EXPAND, FLAT}
           — 마지막 완성 3분봉 3개(g2,g1,g0)로 production mu_assist.hy_gap_state 그대로.
결과     : MU 방향 ETF, MU 플래그봉 종료 분 시가 +1틱 진입. 15/30분 수익률, ±1.5% 선터치(같은 분 둘 다면 손실 우선).
"""
import csv, sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

WT = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad/wt18")
sys.path.insert(0, str(WT))
sys.stdout.reconfigure(encoding="utf-8")
from app.trading.macd2 import config, mu_assist, signal_engine as se, teg_gate  # noqa: E402

KST = config.KST
S46 = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/46f5b540-1e0b-4dc8-b0bf-faf804cf00b0/scratchpad")
SBB = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad")
D4 = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/abf63e6d-d10e-4d96-ad22-c76642c87888/scratchpad/d1104")
DATA = [D4 / "sp_1104/data", SBB / "data", S46 / "rsp/data", SBB / "rsp/data"]
RT = {"20261103": SBB / "mu/rt", "20261104": D4 / "rt"}
DAYS = "20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030 20261102 20261103 20261104".split()
ALL = sorted({f.stem.split("_")[1] for d in DATA for f in d.glob("replay_*_hynix_1m.csv")})
OUT = Path(__file__).with_name("mu_state.csv")


def bars(day, tag):
    for d in DATA:
        p = d / f"replay_{day}_{tag}_1m.csv"
        if p.exists():
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
            return x[x["datetime"].dt.strftime("%Y%m%d") == day].reset_index(drop=True)
    return None


def crosses(ms):
    d = (ms["macd"] - ms["signal"]).values
    out = []
    for k in range(1, len(d)):
        if d[k - 1] <= 0 < d[k]:
            out.append((k, "RED"))
        elif d[k - 1] >= 0 > d[k]:
            out.append((k, "BLUE"))
    return out


def cttr_mean(day, end):
    root = RT.get(day, S46 / "mu_sp/rt")
    p = root / "snapshots" / f"{day[:4]}-{day[4:6]}-{day[6:]}" / "000660.csv"
    if not p.exists():
        return None
    a, b = (end - timedelta(seconds=60)).isoformat(), end.isoformat()
    v = []
    with open(p, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = r["ts"][:19]
            if t >= b:
                break
            if t >= a and r.get("cttr"):
                v.append(float(r["cttr"]))
    return sum(v) / len(v) if v else None


rows = []
for day in DAYS:
    i = ALL.index(day)
    hy = pd.concat([bars(ALL[i - 2], "hynix"), bars(ALL[i - 1], "hynix"), bars(day, "hynix")]).reset_index(drop=True)
    hy, _ = se.exclude_preopen_padding_1m(hy)
    d0 = datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST)
    hb = se.resample_completed_3m(hy, d0.replace(hour=15, minute=30))
    hm = se.calculate_macd_series(hb)
    hm["g"] = hm["macd"] - hm["signal"]
    hflags = [(hm["datetime"].iloc[k] + timedelta(minutes=3), dr) for k, dr in crosses(hm)]   # 확정 시각
    mu = mu_assist.load_mu_1m(datetime.strptime(day, "%Y%m%d").replace(hour=16), str(RT.get(day, S46 / "mu_sp/rt")))
    if mu is None or len(mu) < 120:
        print(day, "MU 없음"); continue
    m3 = mu.resample("3min", label="left", closed="left").last().dropna()
    mm = se.calculate_macd_series(pd.DataFrame({"datetime": m3.index, "close": m3.values}))
    etf = {"RED": bars(day, "long"), "BLUE": bars(day, "inverse")}
    for k, dr in crosses(mm):
        t = (pd.Timestamp(mm["datetime"].iloc[k]) + timedelta(minutes=3)).tz_localize(KST)   # MU 플래그 확정
        if not (d0.replace(hour=9, minute=3) <= t <= d0.replace(hour=14, minute=50)):
            continue
        prev = [f for f in hflags if f[0] <= t and f[0] >= t - timedelta(minutes=90)]
        same = [f for f in prev if f[1] == dr]
        last_same = same[-1][0] if same else None
        opp_after = bool(last_same) and any(f[1] != dr and f[0] > last_same for f in hflags if f[0] <= t)
        sgn = 1 if dr == "RED" else -1
        done = hm[hm["datetime"] + timedelta(minutes=3) <= t]
        g2, g1, g0 = (sgn * float(x) for x in done["g"].iloc[-3:])
        hs = mu_assist.hy_gap_state(g2, g1, g0)
        c = cttr_mean(day, t.replace(tzinfo=None))
        cm = None if c is None else ((c > 100) == (dr == "RED"))
        hbt = hb[hb["datetime"] + timedelta(minutes=3) <= t].reset_index(drop=True)
        teg = teg_gate.evaluate_teg(hbt, "UP_RED" if dr == "RED" else "DOWN_BLUE", hbt["datetime"].iloc[-2], t)
        vw = teg.conditions.get(teg_gate.COND_VWAP); em = teg.conditions.get(teg_gate.COND_EMA_STACK)
        gap_ok = g0 > 0 and hs in ("EXPAND", "FLAT")
        ve_ok = not (vw is False and em is False)          # 둘 다 반대일 때만 '심하게 반대'
        grp = "A" if (gap_ok and cm and ve_ok) else "B"
        e = etf[dr]
        seg = e[e["datetime"] >= t].reset_index(drop=True)
        if seg.empty:
            continue
        ent = float(seg["open"].iloc[0]) + 5
        r_at = lambda mins: (float(seg[seg["datetime"] < t + timedelta(minutes=mins)]["close"].iloc[-1]) / ent - 1) * 100
        first = "NONE"
        for _, b in seg[seg["datetime"] <= d0.replace(hour=15, minute=19)].iterrows():
            lo, hi = float(b["low"]) <= ent * 0.985, float(b["high"]) >= ent * 1.015
            if lo:
                first = "-1.5"; break
            if hi:
                first = "+1.5"; break
        rows.append(dict(date=day, mu_flag=t.strftime("%H:%M"), dir=dr, group=grp,
                         hy_same_flag=last_same.strftime("%H:%M") if last_same else "",
                         elapsed_min=int((t - last_same).total_seconds() // 60) if last_same else "",
                         hy_opposite_after=opp_after, cttr=None if c is None else round(c, 1), cttr_match=cm,
                         hy_gap=round(g0, 1), hy_gap_state=hs, gap_ok=gap_ok, vwap_ok=vw, ema_stack_ok=em, ve_ok=ve_ok, ret15=round(r_at(15), 2), ret30=round(r_at(30), 2),
                         first_touch=first, est_mu_gap_day=day >= "20261102"))
    print(day, "MU 플래그", sum(1 for r in rows if r["date"] == day))

with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)


def summ(rs):
    if not rs:
        return "0건"
    n = len(rs)
    return (f"{n}건 승률(30분>0) {sum(r['ret30'] > 0 for r in rs)/n*100:.0f}% 평균15분 {sum(r['ret15'] for r in rs)/n:+.2f}% "
            f"평균30분 {sum(r['ret30'] for r in rs)/n:+.2f}% +1.5선터치 {sum(r['first_touch'] == '+1.5' for r in rs)} / "
            f"-1.5선터치 {sum(r['first_touch'] == '-1.5' for r in rs)} / 미터치 {sum(r['first_touch'] == 'NONE' for r in rs)}")


print()
for lab, f in (("A 전부(gap∧체결강도∧VWAP/EMA)", lambda r: r["group"] == "A"), ("B 나머지", lambda r: r["group"] == "B"),
               ("참고: gap∧VWAP/EMA (체결강도 무관)", lambda r: r["gap_ok"] and r["ve_ok"]),
               ("참고: gap 만", lambda r: r["gap_ok"]), ("참고: 전체 MU 플래그", lambda r: True)):
    for dr in ("RED", "BLUE", None):
        print(f"[{lab}] {dr or '전체':<4} {summ([r for r in rows if f(r) and (dr is None or r['dir'] == dr)])}")
print("[A 전수]")
for r in rows:
    if r["group"] == "A":
        print(" ", {k: r[k] for k in ("date", "mu_flag", "dir", "hy_same_flag", "cttr", "hy_gap", "hy_gap_state", "vwap_ok", "ema_stack_ok", "ret15", "ret30", "first_touch")})
print("[11/04 전부]")
for r in rows:
    if r["date"] == "20261104":
        print(" ", {k: r[k] for k in ("mu_flag", "dir", "group", "hy_same_flag", "cttr", "cttr_match", "hy_gap", "hy_gap_state", "vwap_ok", "ema_stack_ok", "ret15", "ret30", "first_touch")})
print("CSV:", OUT)
