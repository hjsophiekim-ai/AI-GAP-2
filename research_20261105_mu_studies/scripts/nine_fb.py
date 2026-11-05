"""09시대 fallback 간단 비교 (재생 없음, 날짜별 순차, 결과 요약만).

B: MU 데이터가 있던 서머타임 날(10/19~10/30)의 09:00~09:59 T+3 후보 중 MU 단계까지 온 것
   (EXECUTED 또는 MU_ASSIST_* 차단) -> HY 체결강도 일치 ∧ HY gap EXPAND(T+2 기록 hy_gap_t2_state) 이면 후보.
C: 08:39~08:48 봉 HY 플래그 ∧ 08:51 시점 MU 3분 MACD 방향 일치 -> 09:06 같은 방향 진입 (10/19~11/04).
결과: 판정 분 ETF 시가+1틱, ±1.5% 선터치(같은 분 둘 다면 손실 우선), 15/30분 수익률.
"""
import csv, sys
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

WT = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad/wt18")
sys.path.insert(0, str(WT)); sys.stdout.reconfigure(encoding="utf-8")
from app.trading.macd2 import config, mu_assist, signal_engine as se  # noqa: E402

KST = config.KST
S46 = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/46f5b540-1e0b-4dc8-b0bf-faf804cf00b0/scratchpad")
SBB = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad")
D4 = Path(r"C:/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/abf63e6d-d10e-4d96-ad22-c76642c87888/scratchpad/d1104")
DATA = [D4 / "sp_1104/data", SBB / "data", S46 / "rsp/data", SBB / "rsp/data"]
RT = {"20261103": SBB / "mu/rt", "20261104": D4 / "rt"}
ALL = sorted({f.stem.split("_")[1] for d in DATA for f in d.glob("replay_*_hynix_1m.csv")})
B_DAYS = "20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030".split()
C_DAYS = B_DAYS + ["20261102", "20261103", "20261104"]


def bars(day, tag, cols=("datetime", "open", "high", "low", "close", "volume")):
    for d in DATA:
        p = d / f"replay_{day}_{tag}_1m.csv"
        if p.exists():
            x = pd.read_csv(p, usecols=list(cols))
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
            return x[x["datetime"].dt.strftime("%Y%m%d") == day].reset_index(drop=True)
    return None


def cttr_match(day, end, dr):
    p = RT.get(day, S46 / "mu_sp/rt") / "snapshots" / f"{day[:4]}-{day[4:6]}-{day[6:]}" / "000660.csv"
    a, b = (end - timedelta(seconds=60)).isoformat(), end.isoformat()
    v = []
    with open(p, encoding="utf-8") as fh:
        r = csv.reader(fh); h = next(r); it, ic = h.index("ts"), h.index("cttr")
        for row in r:
            t = row[it][:19]
            if t >= b:
                break
            if t >= a and row[ic]:
                v.append(float(row[ic]))
    if not v:
        return None
    return (sum(v) / len(v) > 100) == (dr == "RED")


def outcome(day, dr, t):
    e = bars(day, "long" if dr == "RED" else "inverse")
    seg = e[e["datetime"] >= t].reset_index(drop=True)
    ent = float(seg["open"].iloc[0]) + 5
    ret = lambda m: (float(seg[seg["datetime"] < t + timedelta(minutes=m)]["close"].iloc[-1]) / ent - 1) * 100
    first = "NONE"
    for lo, hi in zip(seg["low"], seg["high"]):
        if lo <= ent * 0.985:
            first = "-1.5"; break
        if hi >= ent * 1.015:
            first = "+1.5"; break
    return round(ret(15), 2), round(ret(30), 2), first


res = {"B": [], "C": []}
for day in B_DAYS:                      # ── B
    bt = S46 / "rsp/bt" / f"FX_{day}" / "test_wk20"
    mu = {r["signal_id"]: r.get("hy_gap_t2_state", "") for r in csv.DictReader(open(bt / "macd2_mu_assist_ledger.csv", encoding="utf-8"))
          if r.get("kind") == "ENTRY_SHADOW"}
    for r in csv.DictReader(open(bt / "macd2_signal_ledger.csv", encoding="utf-8")):
        sid, t0 = r["signal_id"], r["detected_at"][11:16]
        if not (sid.startswith(day) and sid.endswith(":TW2_3SLOT_CONFIRM") and "09:00" <= t0 < "10:00"):
            continue
        if not (r["order_result"] == "EXECUTED" or r["block_reason"].startswith("MU_ASSIST")):
            continue
        dr = "RED" if "_UP_RED" in sid else "BLUE"
        t = datetime.strptime(day + t0, "%Y%m%d%H:%M").replace(tzinfo=KST)
        cm = cttr_match(day, t.replace(tzinfo=None), dr)
        hs = mu.get(sid, "")
        if cm and hs == "EXPAND":
            res["B"].append((day, t0, dr, *outcome(day, dr, t), "실진입" if r["order_result"] == "EXECUTED" else r["block_reason"]))
for day in C_DAYS:                      # ── C
    i = ALL.index(day)
    hy = pd.concat([bars(d, "hynix", ("datetime", "open", "high", "low", "close")) for d in (ALL[i - 2], ALL[i - 1], day)]).reset_index(drop=True)
    hy, _ = se.exclude_preopen_padding_1m(hy)
    d0 = datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST)
    hb = se.resample_completed_3m(hy, d0.replace(hour=9, minute=0))
    hm = se.calculate_macd_series(hb); g = (hm["macd"] - hm["signal"]).values
    fl = [("RED" if g[k] > 0 else "BLUE", hm["datetime"].iloc[k]) for k in range(1, len(g))
          if (g[k - 1] <= 0 < g[k] or g[k - 1] >= 0 > g[k]) and d0.replace(hour=8, minute=39) <= hm["datetime"].iloc[k] <= d0.replace(hour=8, minute=48)]
    if not fl:
        continue
    dr, ft = fl[-1]
    mu = mu_assist.load_mu_1m(datetime.strptime(day, "%Y%m%d").replace(hour=8, minute=51), str(RT.get(day, S46 / "mu_sp/rt")))
    if mu is None or len(mu) < 120:
        continue
    m = mu[mu.index + pd.Timedelta(minutes=1) <= datetime.strptime(day, "%Y%m%d").replace(hour=8, minute=51)]
    if len(m) < int(config.MU_ASSIST_MIN_BARS):
        print("  C:", day, "08:51 MU 1분봉 부족(월요일 등) -> 제외"); continue
    # 로거는 08:00 부터만 MU 를 받아 3분 MACD(26봉) 는 08:51 에 계산 불가 -> production MU Assist 와 같은 1분 MACD hist 부호
    mu_dir = "RED" if float(mu_assist._macd_hist(m).iloc[-1]) > 0 else "BLUE"
    if mu_dir == dr:
        res["C"].append((day, ft.strftime("%H:%M"), dr, *outcome(day, dr, d0.replace(hour=9, minute=6)), "09:06"))


def summ(rs):
    if not rs:
        return "0건"
    n = len(rs)
    return (f"{n}건 선터치 +1.5:{sum(x[5] == '+1.5' for x in rs)} / -1.5:{sum(x[5] == '-1.5' for x in rs)} "
            f"평균15 {sum(x[3] for x in rs)/n:+.2f}% 평균30 {sum(x[4] for x in rs)/n:+.2f}%")


for k in ("B", "C"):
    print(f"[{k}] 전체 {summ(res[k])} | RED {summ([x for x in res[k] if x[2] == 'RED'])} | BLUE {summ([x for x in res[k] if x[2] == 'BLUE'])}")
    for x in res[k]:
        print("   ", x)
print("[A] 현행: 겨울 09시대 MU_UNAVAILABLE -> 진입 0건(손익 0)")
