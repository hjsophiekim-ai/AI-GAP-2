"""10/28 09:51(재생 09:48:40) 거래 — 전략별 진입 게이트 1:1 대조.

    python .../p1_parity.py 20261028 --flag 093900 --flag 092100

플래그봉 하나에 대해 T(플래그봉 완성) → T+1/T+2 → T+3(확인봉 완성) 구간의
TW / TW2 veto / TEG / 슬롯 / MU Assist / MU CATCH-UP / XS / 체결강도 / E 돌파대기 /
최종 체결을 변형별로 나란히 찍는다. 전부 재생 산출(json)의 기록에서만 읽는다.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))

TAGS = [
    ("CHOP+MUA+XS", "P0_CHOP_MUA_XS"),
    ("CHOP+MUA,XS OFF", "P0_CHOP_MUA_NOXS"),
    ("CHOP+MUA+XS,CU kill", "P0_CHOP_MUA_XS_NOCU"),
    ("CHOP 현행(MUA OFF)", "CHK_CHOP_BASE"),
    ("N1", "P0_N1"),
    ("N1+MUA+XS", "P0_N1_MUA_XS"),
    ("P3", "P0_P3"),
    ("E", "P0_E"),
]


def hhmm(t):
    return (t or "")[11:19]


def one(name, path, day, flags):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    g = [x for x in d.get("gates", []) if x["t"][:10].replace("-", "") == day]
    sig = d.get("sig", [])
    brk = d.get("brk", [])
    print(f"\n{'─'*100}\n■ {name}   (mode={d['arm']['mode']} MU={d['arm']['mua']} XS={d['arm']['xs']} "
          f"CUkill={d['arm']['cukill']} tick={d['arm']['tickpx']})")
    print(f"  주문: " + " | ".join(f"{hhmm(o['t'])} {o['side']} {o['sym']} {o['qty']}@{o['px']:,.0f} "
                                   f"{','.join(o['acts'])}" for o in d["orders"]) or "  주문 없음")
    for f in flags:
        rows = [r for r in sig if r["completed_bar_at"] == f]
        if not rows:
            print(f"  [{f[:2]}:{f[2:4]} 플래그] 신호원장에 없음")
            continue
        dirn = rows[0]["direction"]
        print(f"  [{f[:2]}:{f[2:4]} {dirn}]")
        for r in rows:
            print(f"     원장 {hhmm(r['detected_at'])}  {r['order_result']:28s} {r['block_reason'] or ''}")
        fb = f"{day[:4]}-{day[4:6]}-{day[6:]} {f[:2]}:{f[2:4]}:00"
        # 이 플래그의 판정 구간 = 플래그봉 완성(T+0) ~ 확인봉 완성(T+3) + 여유 1분
        t0 = f"{day[:4]}-{day[4:6]}-{day[6:]}T{f[:2]}:{f[2:4]}:00"
        t3 = f"{day[:4]}-{day[4:6]}-{day[6:]}T{int(f[:2]) + (int(f[2:4]) + 7) // 60:02d}:{(int(f[2:4]) + 7) % 60:02d}:00"
        own = (lambda x: (str(x.get("flag_bar") or "").startswith(fb)
                          or str(x.get("sid") or "").startswith(f"{day}_{f}")
                          or str((x.get("res") or {}).get("flag_bar_at", "")).startswith(fb.replace(" ", "T"))))
        rel = [x for x in g if own(x) or (t0 <= x["t"][:19] <= t3)]
        seen = set()
        for x in rel:
            k = (x["t"], x["ev"], json.dumps(x, sort_keys=True))
            if k in seen:
                continue
            seen.add(k)
            ev = x["ev"]
            if ev == "TW":
                print(f"     {hhmm(x['t'])} TW          approved={x['approved']} {x.get('block') or ''}"
                      f"{' [확인검사 생략]' if x.get('skip_conf') else ''}"
                      f"  gap_flag={(x.get('metrics') or {}).get('gap_flag')} gap_now={(x.get('metrics') or {}).get('gap_now')}")
            elif ev == "TW2VETO":
                print(f"     {hhmm(x['t'])} TW2 veto    vetoed={x['vetoed']} {x.get('reason') or ''}")
            elif ev == "TEG":
                print(f"     {hhmm(x['t'])} TEG         approved={x['approved']} {x.get('reasons')}")
            elif ev == "QUALITY":
                print(f"     {hhmm(x['t'])} Quality     passed={x['passed']} approved={x['approved']}")
            elif ev == "SLOT":
                print(f"     {hhmm(x['t'])} 슬롯        allowed={x['allowed']} slot={x.get('slot')} "
                      f"{x.get('session')} cap={x.get('cap')} used={x.get('used')} {x.get('reject') or ''}")
            elif ev == "XSTRENGTH":
                print(f"     {hhmm(x['t'])} 체결강도    {x.get('state')} cttr={x.get('cttr')}")
            elif ev == "XS_STRONG":
                print(f"     {hhmm(x['t'])} XS STRONG   {x.get('strong')} cttr={x.get('cttr')}")
            elif ev == "XS_TEG_OVERRIDE":
                print(f"     {hhmm(x['t'])} XS TEG 완화 applied={x.get('applied')}")
            elif ev == "MU_CATCHUP":
                r = x.get("res") or {}
                print(f"     {hhmm(x['t'])} MU CATCHUP  {r.get('status')} {r.get('reason') or ''} "
                      f"MU={r.get('mu_session_pct')}% HY={r.get('hy_session_pct')}% 괴리={r.get('mu_hy_gap')}%p "
                      f"cttr={r.get('cttr')}")
            elif ev == "MU_ENTRY_STATUS":
                print(f"     {hhmm(x['t'])} MU 진입판정 {x.get('status')}")
            elif ev == "MU_ENTRY_VETO":
                print(f"     {hhmm(x['t'])} MU veto     {x.get('veto')}")
            elif ev == "DECISION":
                print(f"     {hhmm(x['t'])} ▶ 최종판정  approved={x['approved']} {x.get('block') or x.get('decision')}")
        for e in brk:
            if f[:4] in str(e.get("t", "")) or True:
                pass
    print("  E 돌파대기:")
    for e in brk:
        print(f"     {hhmm(e['t'])} {e['ev']} " + (f"trigger={e.get('trigger')} reason={e.get('reason')}"
                                                   if e["ev"] == "e_early_pass" else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--flag", action="append", default=[])
    a = ap.parse_args()
    for name, tag in TAGS:
        p = OUT / f"{tag}_{a.day}.json"
        if not p.exists():
            print(f"\n■ {name}: 산출 없음 ({p.name})")
            continue
        one(name, p, a.day, a.flag)


if __name__ == "__main__":
    main()
