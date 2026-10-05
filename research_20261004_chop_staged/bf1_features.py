"""BOX-FAIL SCORE 1단계 — 상태전이 feature 생성. 미래정보 없음. READ-ONLY.

각 확정 플래그의 **T+3 확정(=진입 결정) 시점**에서, 그 시점 이전 완성봉만으로 6개를 만든다.
BOX = 플래그봉 시작 직전 30개 1분봉의 고가/저가.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
DATA = REPO + "/research_20261002_sept_strategy_replay/data"
KST = "Asia/Seoul"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

PREV = {}
for l in open(O3 + "/../jobs4.txt"):
    p = l.split()
    if len(p) >= 5 and p[1] == "A":
        PREV[p[2]] = (p[3], p[4])
PREV.setdefault("20261002", ("20261001", "20260930"))
_c = {}


def hy(day):
    if day not in _c:
        parts = []
        for x in (*PREV.get(day, ()), day):
            try:
                d = pd.read_csv(f"{DATA}/replay_{x}_hynix_1m.csv")
                d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
                parts.append(d)
            except FileNotFoundError:
                pass
        _c[day] = pd.concat(parts).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _c[day]


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def box_fail(day, t, flag_dt, up):
    """t = T+3 확정(진입) 시각, flag_dt = 플래그봉 시작."""
    d = hy(day)
    done = d[d["datetime"] + pd.Timedelta(minutes=1) <= t]
    pre = done[done["datetime"] < flag_dt]                     # 박스: 플래그봉 **이전**
    if len(pre) < 50 or len(done) < 60:
        return None
    b = pre.iloc[-30:]
    hi, lo = float(b["high"].max()), float(b["low"].min())
    if hi <= lo:
        return None
    px = float(done["close"].iloc[-1])
    width = hi - lo
    s = 1.0 if up else -1.0

    # ① BOX POSITION — 플래그 방향 가장자리에 얼마나 가까운가 (1=돌파선, 0=반대끝)
    pos = (px - lo) / width if up else (hi - px) / width
    box_pos = float(pos)

    # ② BREAKOUT FAILURE — 최근 30분, 직전 20봉 롤링박스를 뚫었다가 2봉 안에 복귀한 횟수
    w = done.iloc[-30:]
    H, L, C = done["high"].to_numpy(float), done["low"].to_numpy(float), done["close"].to_numpy(float)
    n = len(done)
    att = fail = 0
    for i in range(n - 30, n):
        if i < 20:
            continue
        rh, rl = H[i - 20:i].max(), L[i - 20:i].min()
        out = (H[i] > rh) if up else (L[i] < rl)
        if not out:
            continue
        att += 1
        back = C[i + 1:i + 3] if i + 1 < n else np.array([])
        edge = rh if up else rl
        if len(back) and ((back < edge).all() if up else (back > edge).all()):
            fail += 1
    brk_fail = float(fail)

    # ③ FOLLOW-THROUGH — 플래그봉 극값 대비 T+3 까지의 실제 확장폭 (ATR 단위)
    fb = done[(done["datetime"] >= flag_dt) & (done["datetime"] < flag_dt + pd.Timedelta(minutes=3))]
    aft = done[done["datetime"] >= flag_dt + pd.Timedelta(minutes=3)]
    atr = float(np.abs(np.diff(C[-30:])).mean()) or 1.0
    if len(fb) and len(aft):
        ext = (float(aft["high"].max()) - float(fb["high"].max())) if up else (float(fb["low"].min()) - float(aft["low"].min()))
    else:
        ext = 0.0
    ext_atr = float(ext / atr)

    # ④ REVERSAL PRESSURE — 확정봉 종가가 플래그봉 중간값 쪽으로 되돌아왔는가
    if len(fb):
        fmid = (float(fb["high"].max()) + float(fb["low"].min())) / 2.0
        rev = (fmid - px) / px * 100 if up else (px - fmid) / px * 100
    else:
        rev = 0.0
    rev = float(rev)

    # ⑤ SWING PERSISTENCE — 최근 30분을 10분 3구간으로 나눠 같은 방향 확장이 몇 번 이어졌나 (0~2)
    segs = [done.iloc[-30:-20], done.iloc[-20:-10], done.iloc[-10:]]
    persist = 0
    for k in (1, 2):
        a, c = segs[k - 1], segs[k]
        if len(a) and len(c):
            if (float(c["high"].max()) > float(a["high"].max())) if up else (float(c["low"].min()) < float(a["low"].min())):
                persist += 1
    persist = float(persist)

    # ⑥ BOX RE-ENTRY — 플래그 후 박스 밖으로 나갔다가 다시 안으로 들어왔는가
    post = done[done["datetime"] >= flag_dt]
    if len(post):
        went = (float(post["high"].max()) > hi) if up else (float(post["low"].min()) < lo)
        inside = (px <= hi) if up else (px >= lo)
        reentry = float(1.0 if (went and inside) else 0.0)
    else:
        reentry = 0.0
    return dict(box_pos=box_pos, brk_fail=brk_fail, ext_atr=ext_atr, rev=rev,
                persist=persist, reentry=reentry, box_w=float(width / px * 100), att=float(att))


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for day in DAYS + ["20261002"]:
    for t in trades(json.load(open(apath(day), encoding="utf-8"))):
        sid = t.get("sid") or ""
        try:
            p = sid.split(":")[0].split("_")
            fdt = pd.Timestamp(f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]} {p[1][:2]}:{p[1][2:4]}:{p[1][4:]}", tz=KST)
        except Exception:
            continue
        up = t["dir"].startswith("UP")
        f = box_fail(day, t["entry"], fdt, up)
        if f is None:
            continue
        rs = "+".join(dict.fromkeys(t["reasons"]))
        f.update(day=day, entry=t["entry"].isoformat(), flag=fdt.isoformat(), up=int(up),
                 krw=t["krw"], net=t["net"], rs=rs,
                 y_sl=int("STOP_LOSS" in rs or rs == "B3_SL"), y_run=int("TP2_FULL" in rs))
        rows.append(f)
df = pd.DataFrame(rows)
df.to_csv(ROOT + "/bf_features.csv", index=False, encoding="utf-8")
tr, te = df[df.day < "20260801"], df[(df.day >= "20260801") & (df.day <= "20261001")]
print(f"# 생성 {len(df)}건 (train {len(tr)} / test {len(te)} / 10-02 {int((df.day=='20261002').sum())})")
print(f"  손절 {int(df.y_sl.sum())} · runner {int(df.y_run.sum())}")
print("\n## train 단변량 방향 (y_sl 기준 AUC — 0.5 보다 크면 '높을수록 손절')")
def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() in (0, len(y)):
        return np.nan
    r = pd.Series(s).rank().to_numpy(); n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))
for c in ("box_pos", "brk_fail", "ext_atr", "rev", "persist", "reentry", "box_w", "att"):
    print(f"  {c:9s} train {auc(tr.y_sl, tr[c]):.3f}  test {auc(te.y_sl, te[c]):.3f}   "
          f"(train 평균 손절 {tr[tr.y_sl==1][c].mean():+.2f} / 비손절 {tr[tr.y_sl==0][c].mean():+.2f})")
