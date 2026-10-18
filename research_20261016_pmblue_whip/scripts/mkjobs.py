"""BASE 원장을 읽어 변형을 돌릴 '영향 가능일' 만 뽑는다 (2026-10-16). READ-ONLY.

연구2 : 13:30~14:30 DOWN_BLUE 확정플래그가 VWAP 관문에서 떨어진 날만.
        (VWAP veto / TEG 거절 — TEG 는 COND_VWAP 때문일 수 있다)
        그 외 사유(슬롯캡·미확정·MACD gap·H50)는 이 변형이 건드릴 수 없는 경로다.
연구3 : s3_whip.py 가 뽑은 오전 휩쏘일. W2(K=2)는 그중 BASE 가 ON 이후 오전에
        2번 이상 진입한 날만(그래야 3번째가 존재할 수 있다).
"""
import json
import os
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anal  # noqa: E402

MY = anal.MY
PMV_REASONS = {"TW2_REJECT_VWAP_VETO", "TW2_3SLOT_REJECT_TEG", "TW_TEG_3SLOT_REJECT_CHOP_TEG"}


def prev(dys_all, d, k):
    i = dys_all.index(d)
    return dys_all[i - k]


def main():
    dys = anal.days()
    allj = [x.split() for x in open(MY + "/jobs_base.txt") if x.strip()]
    pmap = {r[1]: (r[2], r[3]) for r in allj}

    pm_days, pm_rows = [], []
    for d in dys:
        p = f"{anal.OUT}/BASE_{d}.json"
        if not os.path.exists(p):
            continue
        o = json.load(open(p, encoding="utf-8"))
        hit = False
        for s in o.get("sig", []):
            sid = str(s.get("signal_id") or "")
            if not sid.endswith(":TW2_3SLOT_CONFIRM") or s.get("direction") != "DOWN_BLUE":
                continue
            t = pd.Timestamp(str(s["detected_at"])[:19])
            if not (pd.Timestamp("13:30").time() <= t.time() < pd.Timestamp("14:30").time()):
                continue
            if str(t.date()).replace("-", "") != d:
                continue
            pm_rows.append(dict(day=d, t=t.strftime("%H:%M"), result=s.get("order_result"),
                                reason=s.get("block_reason")))
            if s.get("order_result") != "EXECUTED" and str(s.get("block_reason")) in PMV_REASONS:
                hit = True
        if hit:
            pm_days.append(d)
    pr = pd.DataFrame(pm_rows)
    print(f"BASE 원장에서 13:30~14:30 DOWN_BLUE 확정플래그 {len(pr)}건 / {pr.day.nunique()}일")
    print(pr.reason.fillna("(통과)").value_counts().to_string())
    print(f"\n연구2 영향 가능일 {len(pm_days)}일: {' '.join(pm_days)}")
    for tag in ("PMRELAX", "PMOFF"):
        with open(f"{MY}/jobs_{tag}.txt", "w") as f:
            f.write("".join(f"{tag} {d} {pmap[d][0]} {pmap[d][1]}\n" for d in pm_days))

    # ── 연구3 ───────────────────────────────────────────────────────────
    wd = [x.split() for x in open(MY + "/whipdays.txt") if x.strip()]
    wd = [(d, on) for d, on in wd if d in dys]
    with open(f"{MY}/jobs_W1.txt", "w") as f:
        f.write("".join(f"W1 {d} {pmap[d][0]} {pmap[d][1]} {on}\n" for d, on in wd))
    # W2: BASE 가 ON 이후 오전에 2번 이상 진입한 날만
    w2 = []
    base = anal.load("BASE")
    for d, on in wd:
        t = base[(base.day == d) & (base.entry.str.replace(":", "") >= on) & (base.entry < "11:00")]
        if len(t) >= 2:
            w2.append((d, on))
    with open(f"{MY}/jobs_W2.txt", "w") as f:
        f.write("".join(f"W2 {d} {pmap[d][0]} {pmap[d][1]} {on}\n" for d, on in w2))
    print(f"\n연구3 W1(오전 1진입 제한) {len(wd)}일, W2(오전 2진입 제한) {len(w2)}일: "
          f"{' '.join(d for d, _ in w2)}")


if __name__ == "__main__":
    main()
