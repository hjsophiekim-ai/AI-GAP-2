"""주간(9/22~10/01) production-replay 진입 위에서 청산 반사실. READ-ONLY.
ETF 1분봉 종가 경로로 판정(봉 확정 후 다음 tick 체결 = 그 봉 종가 근사). 수수료 왕복 0.03% 차감.
보유가 길어지면 뒤 진입과 겹친다 -> 같은 방향 진입은 버리고, 반대방향 진입은 '전환'(보유분 청산 후 진입)으로 처리.
"""
import json, sys
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]; DAYS = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
FEE = 0.03
LONG, INV = "0193T0", "0197X0"
def etf(d, sym):
    tag = "long" if sym == LONG else "inverse"
    x = pd.read_csv(f"{S}/wk/data/replay_{d}_{tag}_1m.csv", parse_dates=["datetime"])
    x["done"] = x["datetime"] + pd.Timedelta(minutes=1)
    return x[x["datetime"].dt.strftime("%Y%m%d") == d].reset_index(drop=True)

def trades_of(o):
    """orders -> 진입별 (진입시각, 종목, 수량가중 진입가, 실제 실현 net%)"""
    out, cur = [], None
    for r in o["orders"]:
        t = pd.Timestamp(r["t"]).tz_localize(None)
        if r["side"] == "BUY":
            cur = dict(t=t, sym=r["sym"], qty=r["qty"], px=r["px"], sold=0, proceeds=0.0, exit_t=None)
            out.append(cur)
        elif cur is not None and r["sym"] == cur["sym"]:
            cur["sold"] += r["qty"]; cur["proceeds"] += r["qty"] * r["px"]; cur["exit_t"] = t
    for c in out:
        c["act"] = (c["proceeds"] / max(c["sold"], 1) / c["px"] - 1) * 100 - FEE if c["sold"] else None
    return out

def opp_times(o, d):
    """반대 플래그가 T+3 을 통과(확인)한 시각 = 그 확인 행 detected_at (NOT_CONFIRMED/GAP 거절 제외)."""
    res = []
    for r in o["sig"]:
        sid = r["signal_id"] or ""
        if ":TW2_3SLOT_CONFIRM" not in sid:
            continue
        if (r["block_reason"] or "") in ("REJECT_NOT_CONFIRMED", "REJECT_MACD_GAP_NOT_EXPANDING"):
            continue
        res.append((pd.Timestamp(r["detected_at"]).tz_localize(None), "UP" if "UP_RED" in sid else "DOWN"))
    return sorted(res)

def sim(path, entry_px, t0, rule, opp, direction, end):
    """rule: dict(sl, tp, hold, trail_arm, trail_give, opp_exit)  -> (exit_t, net%)"""
    peak = 0.0
    for _, b in path.iterrows():
        t = b["done"]
        if t <= t0:
            continue
        r = (b["close"] / entry_px - 1) * 100
        lo = (b["low"] / entry_px - 1) * 100
        peak = max(peak, (b["high"] / entry_px - 1) * 100)
        if rule.get("sl") is not None and r <= -rule["sl"]:
            return t, r - FEE
        if rule.get("tp") is not None and r >= rule["tp"]:
            return t, r - FEE
        if rule.get("hold") is not None and (t - t0).total_seconds() >= rule["hold"] * 60:
            return t, r - FEE
        if rule.get("trail_arm") is not None and peak >= rule["trail_arm"] and r <= peak - rule["trail_give"]:
            return t, r - FEE
        if rule.get("opp_exit") and any(t0 < ot <= t and od != direction for ot, od in opp):
            return t, r - FEE
        if t >= end:
            return t, r - FEE
    b = path.iloc[-1]; return b["done"], (b["close"] / entry_px - 1) * 100 - FEE

RULES = {
    "X1 15:00까지 보유": dict(),
    "X2 반대확인 플래그까지": dict(opp_exit=True),
    "X3 X2+손절2%": dict(opp_exit=True, sl=2.0),
    "X4 X2+손절1%": dict(opp_exit=True, sl=1.0),
    "X5 손절1%+2%후 1.5%반납 trail+반대": dict(opp_exit=True, sl=1.0, trail_arm=2.0, trail_give=1.5),
    "X6 손절1.5%+반대": dict(opp_exit=True, sl=1.5),
    "X7 B3(±1%/20분) 단순": dict(sl=1.0, tp=1.0, hold=20.0),
}
tot = {k: 0.0 for k in ["X0 실제(P3 replay)"] + list(RULES)}
for d in DAYS:
    o = json.load(open(f"{S}/wk/out/wk_{d}.json", encoding="utf-8"))
    tr = trades_of(o); opp = opp_times(o, d)
    end = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00")
    print(f"\n=== {d}  진입 {len(tr)}건  반대확인 플래그 {[(t.strftime('%H:%M'), x) for t, x in opp]}")
    for c in tr:
        print(f"  {c['t']:%H:%M} {'레버' if c['sym']==LONG else '인버'} {c['qty']}@{c['px']:.0f} -> {c['exit_t']:%H:%M} 실제 {c['act']:+.2f}%")
    tot["X0 실제(P3 replay)"] += sum(c["act"] or 0 for c in tr)
    for name, rule in RULES.items():
        held_until, held_dir, s, log = None, None, 0.0, []
        for c in tr:
            dirn = "UP" if c["sym"] == LONG else "DOWN"
            if held_until is not None and c["t"] < held_until:
                if dirn == held_dir or not rule.get("opp_exit"):
                    log.append(f"{c['t']:%H:%M}skip"); continue
            path = etf(d, c["sym"])
            xt, net = sim(path, c["px"], c["t"], rule, opp, dirn, end)
            held_until, held_dir = xt, dirn
            s += net; log.append(f"{c['t']:%H:%M}->{xt:%H:%M} {net:+.2f}")
        tot[name] += s
        print(f"  {name:32s} 합 {s:+6.2f}%  | " + ", ".join(log))
print("\n=== 6일 합계 (진입별 net% 단순합, 원금 비례 아님)")
for k, v in tot.items():
    print(f"  {k:32s} {v:+7.2f}%")
