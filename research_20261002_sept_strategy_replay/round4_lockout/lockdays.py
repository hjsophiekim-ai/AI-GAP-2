"""A(P3-R0) 재생의 T+3 확인 신호로 각 lockout 규칙의 발동일을 미리 찾는다 (T+3 판정은 포지션과 무관)."""
import json, glob, os, sys
import pandas as pd
S = sys.argv[1]
NOT_T3 = {"REJECT_NOT_CONFIRMED", "REJECT_MACD_GAP_NOT_EXPANDING"}

def confirmed(o):
    out = []
    for s in o["sig"]:
        sid = s["signal_id"] or ""
        if ":TW2_3SLOT_CONFIRM" not in sid or (s["block_reason"] or "") in NOT_T3 or (s["order_result"] or "") == "TIME_WINDOW_WHIPSAW_HOLD":
            continue
        out.append((pd.Timestamp(s["detected_at"]), 1 if "UP_RED" in sid else -1, sid.split(":")[0]))
    out.sort()
    ded, seen = [], set()
    for x in out:
        if x[2] not in seen:
            seen.add(x[2]); ded.append(x)
    return ded

def lock_windows(sig, rule):
    wins, until = [], None
    for i, (t, f, sid) in enumerate(sig):
        if until is not None and t < until:
            continue                      # 차단 중 신호는 판정 이력엔 남지만 재발동은 차단 종료 뒤에만
        hist = sig[: i + 1]
        flips = [hist[j][0] for j in range(1, len(hist)) if hist[j][1] != hist[j - 1][1]]
        trig = False
        if rule == "L30":
            trig = sum(1 for x in flips if x >= t - pd.Timedelta(minutes=30)) >= 2
        elif rule == "L45":
            trig = sum(1 for x in flips if x >= t - pd.Timedelta(minutes=45)) >= 3
        elif rule == "ALT3":
            trig = len(hist) >= 3 and hist[-1][1] == hist[-3][1] != hist[-2][1]
        if trig:
            until = t + pd.Timedelta(minutes=30)
            wins.append((t, until))
    return wins

if __name__ == "__main__":
    res = {}
    for p in sorted(glob.glob(S + "/wk/out4/REAL_A_*.json")):
        d = os.path.basename(p)[7:15]
        sig = confirmed(json.load(open(p, encoding="utf-8")))
        for r in ("L30", "L45", "ALT3"):
            w = lock_windows(sig, r)
            if w:
                res.setdefault(r, {})[d] = [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in w]
    for r in ("L30", "L45", "ALT3"):
        print(r, "발동일", len(res.get(r, {})), "발동", sum(len(v) for v in res.get(r, {}).values()))
    json.dump(res, open(S + "/wk/lab/lockdays.json", "w"), indent=1)
