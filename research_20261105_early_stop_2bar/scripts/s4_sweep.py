"""30분 경계 민감도 — WK_SLHOLD_MIN 스윕 집계.

판정이 바뀌는 날만 재생했다. 재생하지 않은 날은 MIN=30 재생(CHK_SLH_ON)을 그대로 쓰되,
**그 날의 유예/통과 판정이 새 임계값에서도 전부 같은 쪽인지 검증**한다
(DEFER 는 held<=MIN, PASS(LATE) 는 held>MIN 이어야 한다). 하나라도 어긋나면
그 날은 재생이 필요하다는 뜻이므로 INVALID 로 표시하고 집계에서 제외하지 않고 경고한다.
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUTD = Path(os.environ.get("ARM3_OUT",
            r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out"))
FEE = 0.000036
FROM = "20260803"
BASE_MIN = 30
SETTINGS = [(10, "SLH_M10"), (15, "SLH_M15"), (25, "SLH_M25"), (BASE_MIN, "CHK_SLH_ON"),
            (35, "SLH_M35"), (45, "SLH_M45"), (120, "SLH_M120")]


def episodes(js, day):
    eps, cur = [], None
    for o in js["orders"]:
        if o["t"][:10].replace("-", "") != day:
            continue
        if o["side"] == "BUY":
            if cur is not None and cur["left"] > 0:
                cur["buys"].append(o); cur["left"] += o["qty"]; continue
            cur = dict(sym=o["sym"], buys=[o], sells=[], left=o["qty"]); eps.append(cur)
        elif cur is not None:
            cur["sells"].append(o); cur["left"] -= o["qty"]
    out = []
    for ep in eps:
        e = float(ep["buys"][0]["px"])
        out.append(sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e)
                       for s in ep["sells"]))
    return out


def pick(tag, day):
    for f in (OUTD / f"{tag}_{day}.json", OUTD / f"CHK_SLH_ON_{day}.json",
              OUTD / f"L85_A_{day}.json"):
        if f.exists():
            return f, f.name.rsplit("_", 1)[0]
    raise SystemExit(f"없음 {day}")


def check_reuse(day, minutes):
    """MIN=30 재생을 재사용해도 되는가 — 그 날 판정이 전부 같은 쪽인지."""
    f = OUTD / f"CHK_SLH_ON_{day}.json"
    if not f.exists():
        return True, ""
    js = json.loads(f.read_text(encoding="utf-8"))
    for x in js.get("slh") or []:
        if x["t"][:10].replace("-", "") != day:
            continue                       # 워밍업일 이벤트
        h = x.get("held")
        if h is None:
            continue
        if x["k"] == "DEFER" and h > minutes:
            return False, f"{x['t'][11:16]} DEFER held={h} > {minutes}"
        if x["k"] == "PASS" and x.get("why") == "LATE" and h <= minutes:
            return False, f"{x['t'][11:16]} PASS(LATE) held={h} <= {minutes}"
    return True, ""


def metrics(per_day, days):
    tr = [p for d in days for p in per_day[d]]
    daily = [sum(per_day[d]) for d in days]
    cum = peak = mdd = 0.0
    for x in daily:
        cum += x; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    g = sum(t for t in tr if t > 0); l = -sum(t for t in tr if t < 0)
    return dict(n=len(tr), total=sum(tr), pf=(g / l if l else float("inf")), mdd=mdd,
                worst=min(daily), worst_tr=min(tr, default=0.0),
                win=100.0 * sum(1 for t in tr if t > 0) / max(len(tr), 1),
                loss_days=sum(1 for x in daily if x < 0))


days = sorted(f.stem.split("_")[-1] for f in OUTD.glob("L85_A_2026*.json")
              if f.stem.split("_")[-1] >= FROM)
A = {d: episodes(json.loads((OUTD / f"L85_A_{d}.json").read_text(encoding="utf-8")), d)
     for d in days}
sa = metrics(A, days)

print(f"WK_SLHOLD_MIN 스윕 — {len(days)}일 {days[0]}~{days[-1]}, 운영구성 CHOP+MU+XS")
print(f"{'MIN':>5s}{'재생일':>7s}{'총손익':>13s}{'Δ vs 현행':>13s}{'PF':>7s}{'MDD':>11s}"
      f"{'최악일':>11s}{'최악거래':>11s}{'승률':>7s}{'손실일':>7s}")
print(f"{'현행':>5s}{'-':>7s}{sa['total']:>+13,.0f}{0:>13,.0f}{sa['pf']:>7.3f}{sa['mdd']:>11,.0f}"
      f"{sa['worst']:>+11,.0f}{sa['worst_tr']:>+11,.0f}{sa['win']:>7.1f}{sa['loss_days']:>7d}")

warn = []
for minutes, tag in SETTINGS:
    per, own = {}, 0
    for d in days:
        f, src = pick(tag, d)
        if f.name.startswith(tag + "_"):
            own += 1
        elif f.name.startswith("CHK_SLH_ON_"):
            ok, why = check_reuse(d, minutes)
            if not ok:
                warn.append(f"MIN={minutes} {d}: {why}")
        per[d] = episodes(json.loads(f.read_text(encoding="utf-8")), d)
    s = metrics(per, days)
    mark = "  <- 현행" if minutes == BASE_MIN else ""
    print(f"{minutes:>5d}{own:>7d}{s['total']:>+13,.0f}{s['total'] - sa['total']:>+13,.0f}"
          f"{s['pf']:>7.3f}{s['mdd']:>11,.0f}{s['worst']:>+11,.0f}{s['worst_tr']:>+11,.0f}"
          f"{s['win']:>7.1f}{s['loss_days']:>7d}{mark}")

print("\n[재사용 검증]", "모든 날 통과" if not warn else "!! 재생 필요한 날이 재사용됨")
for w in warn:
    print("  !!", w)
