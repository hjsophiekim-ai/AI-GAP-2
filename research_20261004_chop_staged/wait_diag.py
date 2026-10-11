"""대기(EARLY_FAIL) 로 간 신호들의 승인 시점 특징 — '기다리면 손해'를 식별할 수 있는가.
재생 불필요. 미래정보 없음(전부 승인 시각 이전 완성봉). READ-ONLY.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
DATA = REPO + "/research_20261002_sept_strategy_replay/data"
KST = "Asia/Seoul"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
PREV = dict(json.load(open(ROOT + "/rs_days.json", encoding="utf-8"))["prev"])
PREV.setdefault("20261002", ("20261001", "20260930"))
_c = {}


def hy(day):
    if day not in _c:
        ps = []
        for x in (*PREV.get(day, ()), day):
            try:
                d = pd.read_csv(f"{DATA}/replay_{x}_hynix_1m.csv")
                d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
                ps.append(d)
            except FileNotFoundError:
                pass
        _c[day] = pd.concat(ps).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _c[day]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}) + ["20261002"]
rows = []
for d in DAYS:
    A = {t["base"]: t for t in trades(json.load(open(ap(d), encoding="utf-8")))}
    o = json.load(open(pb(d), encoding="utf-8"))
    B = {t["base"].replace(":BRK", ""): t for t in trades(o)}
    ev = o.get("brk") or []
    fail = [e for e in ev if e["ev"] == "EARLY_FAIL"]
    fired = {e["sid"]: e for e in ev if e["ev"] == "FIRED" and e.get("executed")}
    exp = {e["sid"] for e in ev if e["ev"].startswith("EXPIRED")}
    df = hy(d)
    for e in fail:
        at = pd.Timestamp(e["armed_at"])
        done = df[(df["datetime"] + pd.Timedelta(minutes=1) <= at) & (df["datetime"].dt.date == at.date())]
        if len(done) < 40:
            continue
        c = done["close"].to_numpy(float)
        up = e["dir"] == "UP_RED"
        s = 1.0 if up else -1.0
        px = c[-1]
        f = dict(day=d, t=at, dirn=("UP" if up else "DN"), dist=e.get("dist"), c2=e.get("c2"), c3=e.get("c3"))
        for n in (3, 5, 10, 20):
            f[f"vel{n}"] = s * (c[-1] - c[-n]) / c[-n] * 100 if len(c) > n else np.nan
        w30 = done.iloc[-30:]
        f["rng30"] = (w30["high"].max() - w30["low"].min()) / px * 100
        f["atr30"] = float(np.abs(np.diff(c[-30:])).mean()) / px * 100
        path = float(np.abs(np.diff(c[-30:])).sum())
        f["pe30"] = abs(c[-1] - c[-30]) / path if path > 0 else np.nan
        # 박스 상단까지 거리를 ATR 단위로
        f["dist_atr"] = (e.get("dist") or 0) / f["atr30"] if f["atr30"] else np.nan
        f["min_open"] = at.hour * 60 + at.minute - 540
        a, b = A.get(e["sid"]), B.get(e["sid"])
        f["fired"] = e["sid"] in fired
        f["expired"] = e["sid"] in exp
        f["delay"] = fired[e["sid"]]["delay_min"] if e["sid"] in fired else np.nan
        f["ak"] = a["krw"] if a else 0.0
        f["bk"] = b["krw"] if b else 0.0
        f["runup"] = ((b["px_in"] - a["px_in"]) / a["px_in"] * 100) if (a and b) else np.nan
        f["imm_better"] = f["ak"] - f["bk"]          # 즉시진입(=A) 이 대기보다 얼마나 나았나
        rows.append(f)
D = pd.DataFrame(rows)
D["seg"] = np.where(D.day < "20260801", "train", np.where(D.day <= "20261001", "test", "1002"))
print(f"# 대기로 간 신호 {len(D)}건 (train {int((D.seg=='train').sum())} / test {int((D.seg=='test').sum())} / 10-02 {int((D.seg=='1002').sum())})")
print(f"  체결 {int(D.fired.sum())} · 폐기 {int(D.expired.sum())}")
print(f"  즉시진입이 나았던 금액 합 {D.imm_better.sum():+,.0f}원 "
      f"(체결분 {D[D.fired].imm_better.sum():+,.0f} / 폐기분 {D[D.expired].imm_better.sum():+,.0f})")

print("\n## 9월 돌파대기 체결 5건 vs 나머지 — 승인시점 지표 비교")
S5 = D[(D.day.str.startswith("202609")) & (D.fired)]
OTH = D[~D.index.isin(S5.index)]
EXPD = D[D.expired]
cols = ["dist", "vel3", "vel5", "vel10", "vel20", "rng30", "atr30", "pe30", "dist_atr", "min_open"]
print(f"| 지표 | 9월체결 5건 | 전체체결 {int(D.fired.sum())}건 | 폐기 {int(D.expired.sum())}건 |")
print("|---|---|---|---|")
for c in cols:
    print(f"| {c} | {S5[c].mean():+.3f} | {D[D.fired][c].mean():+.3f} | {EXPD[c].mean():+.3f} |")

print("\n## 9월 5건 개별")
print("| 일자 | 진입 | 방향 | dist | c2 | vel3 | vel5 | vel10 | atr30 | pe30 | 상승률 | 즉시가 나았던 금액 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in S5.sort_values("imm_better", ascending=False).iterrows():
    print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']:%H:%M} | {r['dirn']} | {r['dist'] if pd.notna(r['dist']) else float('nan'):.3f} | "
          f"{r['c2']} | {r['vel3']:+.3f} | {r['vel5']:+.3f} | {r['vel10']:+.3f} | {r['atr30']:.3f} | {r['pe30']:.2f} | "
          f"{r['runup']:+.2f}% | **{r['imm_better']:+,.0f}** |")

print("\n## 폐기된 신호들(즉시진입하면 안 되는 것들) 중 손실 상위")
E = EXPD.sort_values("ak").head(8)
print("| 일자 | 진입 | 방향 | dist | c2 | vel3 | vel5 | vel10 | atr30 | pe30 | 즉시진입했다면 |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in E.iterrows():
    print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']:%H:%M} | {r['dirn']} | {r['dist']:.3f} | {r['c2']} | "
          f"{r['vel3']:+.3f} | {r['vel5']:+.3f} | {r['vel10']:+.3f} | {r['atr30']:.3f} | {r['pe30']:.2f} | **{r['ak']:+,.0f}** |")

print("\n## 판별력: '즉시진입이 나았다(imm_better>0)' 를 승인시점 지표로 맞출 수 있는가")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    m = ~np.isnan(s)
    y, s = y[m], s[m]
    if y.sum() in (0, len(y)):
        return np.nan
    r = pd.Series(s).rank().to_numpy(); n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


D["y"] = (D.imm_better > 0).astype(int)
tr, te = D[D.seg == "train"], D[D.seg == "test"]
print(f"| 지표 | train AUC | test AUC | 방향일치 |")
print("|---|---|---|---|")
for c in cols + ["c2", "c3"]:
    x = D[c].astype(float) if c in ("c2", "c3") else D[c]
    a1 = auc(tr.y, tr[c].astype(float)); a2 = auc(te.y, te[c].astype(float))
    ok = "O" if (not np.isnan(a1) and not np.isnan(a2) and (a1 - .5) * (a2 - .5) > 0) else "X"
    print(f"| {c} | {a1:.3f} | {a2:.3f} | {ok} |")
D.to_csv(ROOT + "/wait_diag.csv", index=False, encoding="utf-8")
