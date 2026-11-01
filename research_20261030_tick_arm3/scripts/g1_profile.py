"""85일 124거래 — **진입 특성별 성과 프로파일** (재생 산출만 사용, 추가 재생 불필요).

지금까지의 모든 판정은 11일 18거래(손실 4건)에서 내려졌다. 85일 표본으로 처음으로
"어떤 종류의 진입이 돈을 벌고 어떤 종류가 잃는가" 를 통계적으로 볼 수 있다.

쓰는 값은 전부 **진입 시점에 production 이 이미 알고 있는 것**이다(미래 데이터 없음):
  regime · trend_ok(n1_adaptive.snapshot) · 슬롯번호 · 세션/시간창 · 품질점수 ·
  E 돌파대기 경유 · 방향(레버리지/인버스) · 진입 시각대 · 그날 몇 번째 거래인가

    python .../g1_profile.py --tag L85_A
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes, mdd  # noqa: E402
import a3_peaks as P  # noqa: E402
import r1_run  # noqa: E402

NAME = {"0193T0": "레버리지", "0197X0": "인버스2X"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="L85_A")
    ap.add_argument("--days", nargs="*", default=None)
    ap.add_argument("--min-n", type=int, default=8, help="이 건수 미만 구간은 참고로만 표시")
    a = ap.parse_args()
    days = a.days or r1_run.complete_days()[-85:]

    rows = []
    for d in days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        ent = {e["t"][11:19]: e for e in j.get("entries", [])}
        dec = {}
        for g in j.get("gates", []):
            if g["ev"] == "DECISION" and g.get("approved"):
                dec[str(g.get("sid") or "")] = g.get("metrics") or {}
        eps = episodes(p, d)
        for i, e in enumerate(eps):
            snap = ent.get(e["entry_t"][11:19], {})
            sid = str(snap.get("sid") or "")
            m = dec.get(sid) or dec.get(sid.split(":E_")[0]) or {}
            hh = int(e["entry_t"][11:13])
            mm = int(e["entry_t"][14:16])
            rows.append(dict(
                day=d, t=e["entry_t"][11:19], sym=e["sym"], pnl=e["pnl"], ret=e["ret_pct"],
                regime=snap.get("regime") or "?",
                trend_ok=bool(snap.get("trend_ok")),
                ebrk=":E_BRK" in sid,
                slot=m.get("slot_number"),
                sess=m.get("session") or "?",
                win=m.get("window") or "?",
                qual=m.get("quality_score"),
                nth=i + 1,
                hour=hh,
                early=(hh * 60 + mm) < 10 * 60,          # 10:00 이전
                sym_name=NAME.get(e["sym"], e["sym"]),
            ))

    def blk(title, keyfn, order=None):
        g = defaultdict(list)
        for r in rows:
            g[keyfn(r)].append(r)
        print(f"\n[{title}]")
        print(f"  {'구간':16s}{'거래':>5s}{'총손익':>14s}{'거래당':>11s}{'PF':>8s}"
              f"{'MDD':>11s}{'승률':>7s}{'상위1제외':>14s}")
        keys = order or sorted(g, key=lambda k: -sum(x["pnl"] for x in g[k]))
        for k in keys:
            es = g.get(k) or []
            if not es:
                continue
            w = [x["pnl"] for x in es if x["pnl"] > 0]
            l = [x["pnl"] for x in es if x["pnl"] <= 0]
            dd = defaultdict(float)
            for x in es:
                dd[x["day"]] += x["pnl"]
            daily = [dd[d] for d in days if d in dd]
            tot = sum(x["pnl"] for x in es)
            pf = (sum(w) / abs(sum(l))) if l and sum(l) else float("inf")
            top = sorted((x["pnl"] for x in es), reverse=True)
            mark = "" if len(es) >= a.min_n else "  (표본부족)"
            print(f"  {str(k)[:16]:16s}{len(es):>5d}{tot:>+14,.0f}{tot/len(es):>+11,.0f}"
                  f"{pf:>8.2f}{mdd(daily):>11,.0f}{len(w)/len(es)*100:>6.1f}%"
                  f"{tot-top[0]:>+14,.0f}{mark}")

    tot = sum(r["pnl"] for r in rows)
    print(f"\n{'='*104}\n[{a.tag}] 진입 특성별 성과 — {len(rows)}거래 · {len(days)}일 · 총 {tot:+,.0f}\n{'='*104}")
    blk("regime 체제", lambda r: r["regime"])
    blk("trend_ok (n1_adaptive.snapshot)", lambda r: f"trend_ok={r['trend_ok']}")
    blk("방향", lambda r: r["sym_name"])
    blk("E 돌파대기 경유", lambda r: f"E_BRK={r['ebrk']}")
    blk("슬롯 번호", lambda r: f"slot={r['slot']}")
    blk("그날 몇 번째 거래", lambda r: f"{r['nth']}번째")
    blk("세션", lambda r: r["sess"])
    blk("시간창", lambda r: r["win"])
    blk("진입 시각대", lambda r: f"{r['hour']:02d}시")
    blk("10시 이전 진입", lambda r: f"early={r['early']}")
    blk("품질점수", lambda r: f"quality={r['qual']}")
    blk("regime × 방향", lambda r: f"{r['regime'][:5]}/{r['sym_name'][:4]}")
    blk("regime × trend_ok", lambda r: f"{r['regime'][:5]}/ok={r['trend_ok']}")


if __name__ == "__main__":
    main()
