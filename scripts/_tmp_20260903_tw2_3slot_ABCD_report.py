"""READ-ONLY 분석/리포트 — 엔진은 _tmp_20260903_tw2_3slot_ABCD_train_oos.py.
매매 판단 로직 없음(집계/통계/선택규칙만).

Profit Lock 후보 선택은 TRAIN 20일에서만 한다: 1순위 TRAIN 복리수익,
동률/근접 시 PF, 그 다음 MDD. 선택된 floor는 OOS에 그대로 적용하고
OOS 결과로 재선택하지 않는다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _tmp_20260903_tw2_3slot_ABCD_train_oos as eng  # noqa: E402

RAW = eng.OUTPUT_DIR / "raw.json"


def metrics(trades: list, n_days: int) -> dict:
    closed = [t for t in trades if t.get("net_pct") is not None]
    closed.sort(key=lambda t: t["exit_time"])
    n = len(closed)
    if n == 0:
        return {"n_trades": 0}
    rets = [t["net_pct"] for t in closed]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]

    simple = sum(rets)
    eq = 1.0
    curve = []
    for r in rets:
        eq *= (1.0 + r / 100.0)
        curve.append(eq)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)

    e = peak = mdd_s = 0.0
    for r in rets:
        e += r
        peak = max(peak, e)
        mdd_s = max(mdd_s, peak - e)
    cpeak, mdd_c = 1.0, 0.0
    for v in curve:
        cpeak = max(cpeak, v)
        mdd_c = max(mdd_c, (cpeak - v) / cpeak * 100.0)

    cur = best = 0
    cur_sum = worst_sum = 0.0
    for r in rets:
        if r <= 0:
            cur += 1
            cur_sum += r
            best = max(best, cur)
            worst_sum = min(worst_sum, cur_sum)
        else:
            cur, cur_sum = 0, 0.0

    srt = sorted(range(n), key=lambda i: rets[i], reverse=True)
    top5 = sum(rets[i] for i in srt[:5])
    top10 = sum(rets[i] for i in srt[:10])
    drop = set(srt[:10])
    rest = [rets[i] for i in range(n) if i not in drop]
    ex10_eq = 1.0
    for r in rest:
        ex10_eq *= (1.0 + r / 100.0)

    by_date = defaultdict(float)
    for t in closed:
        by_date[t["date"]] += t["net_pct"]

    return {
        "n_trades": n, "trades_per_day": round(n / n_days, 3),
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "wins": len(wins), "losses": len(losses),
        "simple_cum_pct": round(simple, 3),
        "compound_cum_pct": round((eq - 1.0) * 100.0, 3),
        "avg_pct_per_trade": round(simple / n, 4),
        "pf": (round(pf, 3) if isinstance(pf, float) and pf != float("inf") else pf),
        "mdd_simple_pp": round(mdd_s, 3), "mdd_compound_pct": round(mdd_c, 3),
        "max_consec_losses": best, "max_consec_loss_sum_pct": round(worst_sum, 3),
        "top5_share_pct": round(top5 / simple * 100.0, 1) if simple else None,
        "top10_share_pct": round(top10 / simple * 100.0, 1) if simple else None,
        "ex_top10_simple_pct": round(simple - top10, 3),
        "ex_top10_compound_pct": round((ex10_eq - 1.0) * 100.0, 3),
        "profit_days": sum(1 for v in by_date.values() if v > 0),
        "loss_days": sum(1 for v in by_date.values() if v < 0),
    }


def sub(trades, dates):
    ds = set(dates)
    return [t for t in trades if t["date"] in ds]


def cell(v, w=13, nd=3):
    if v is None:
        return " " * (w - 1) + "-"
    if isinstance(v, float):
        return f"{v:>{w}.{nd}f}"
    return f"{str(v):>{w}}"


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    dates, train, oos = raw["period"]["all"], raw["period"]["train"], raw["period"]["oos"]
    A, B = raw["A"], raw["B"]

    print("=" * 122)
    print("TW2 3-SLOT 개선안 A/B/C/D — READ-ONLY 비교 (production 무수정)")
    print(f"{len(dates)}영업일 {dates[0]}~{dates[-1]}  |  TRAIN {train[0]}~{train[-1]} (20일)  "
          f"|  OOS {oos[0]}~{oos[-1]} (10일)")
    print("공통: PRE15 승계 OFF, 하루 최대 3회(TW2_3SLOT_DAILY_CAP=3), MACD zero-cross/T+3/")
    print("      TW2 veto/TEGv2/whipsaw/TP1·TP2/trailing/손절/수수료·체결가 = production 그대로")
    print(f"CHOP 정의: 1차 TRAIN 확정본 그대로 재사용 ({raw['chop_cfg']}) — 재조정 없음")
    print("=" * 122)

    # ── 1) Profit Lock floor 선택 (TRAIN 전용) ──────────────────────────
    print("\n[1] Profit Lock 후보 P1/P2/P3 — TRAIN 20일에서만 선택")
    print("    (1순위 TRAIN 복리, 근접 시 PF → MDD. OOS로 재선택하지 않음)")
    print(f"{'후보':<28}{'lock발동':>9}{'TRAIN복리%':>12}{'TRAIN PF':>10}"
          f"{'TRAIN MDD%':>12}{'TRAIN승률%':>11}{'(참고)OOS복리%':>15}")
    names = {"0.3": "P1 +1.5%도달→최소+0.3%", "0.5": "P2 +1.5%도달→최소+0.5%",
             "0.8": "P3 +1.5%도달→최소+0.8%"}
    train_rows = {}
    for f in ("0.3", "0.5", "0.8"):
        c = raw["C"][f]
        mt = metrics(sub(c, train), len(train))
        mo = metrics(sub(c, oos), len(oos))
        nfired = sum(1 for t in sub(c, train) if t["lock_fired"])
        train_rows[f] = mt
        print(f"{names[f]:<28}{nfired:>9}{cell(mt['compound_cum_pct'],12)}{cell(mt['pf'],10)}"
              f"{cell(mt['mdd_compound_pct'],12)}{cell(mt['win_rate_pct'],11,2)}"
              f"{cell(mo['compound_cum_pct'],15)}")
    ma = metrics(sub(A, train), len(train))
    print(f"{'(기준) A 현행':<28}{'-':>9}{cell(ma['compound_cum_pct'],12)}{cell(ma['pf'],10)}"
          f"{cell(ma['mdd_compound_pct'],12)}{cell(ma['win_rate_pct'],11,2)}"
          f"{cell(metrics(sub(A,oos),len(oos))['compound_cum_pct'],15)}")

    best_f = max(("0.3", "0.5", "0.8"),
                 key=lambda f: (round(train_rows[f]["compound_cum_pct"], 3),
                                train_rows[f]["pf"] or 0,
                                -train_rows[f]["mdd_compound_pct"]))
    print(f"\n>>> TRAIN 선택: {names[best_f]}  (TRAIN 복리 {train_rows[best_f]['compound_cum_pct']}%)")
    print("    이 floor를 OOS에 그대로 적용한다.")

    C, D = raw["C"][best_f], raw["D"][best_f]
    Cf = raw["C_frozen"][best_f]

    # 진입 동결본과 full-chain이 일치하는지 (C는 청산만 바꾸므로 일치해야 정상)
    k = lambda ts: [(t["entry_time"], t["entry_symbol"], t["exit_time"], round(t["net_pct"], 6))
                    for t in ts]
    c_consistent = (k(C) == k(Cf))
    print(f"    검증: C full-chain == C 진입동결본 → {c_consistent} "
          f"(C는 청산만 변경하므로 진입 집합이 A와 동일: {len(C)}건 vs A {len(A)}건)")

    # ── 2) A/B/C/D 지표표 ────────────────────────────────────────────────
    variants = [("A 현행", A), ("B Slot1TQ4", B), (f"C Lock{best_f}", C), ("D B+C", D)]
    rowspec = [
        ("거래수", "n_trades", 0), ("일평균 거래수", "trades_per_day", 3),
        ("승/패", None, 0), ("승률 %", "win_rate_pct", 2),
        ("단순수익 %", "simple_cum_pct", 3), ("복리수익 %", "compound_cum_pct", 3),
        ("평균수익/거래 %", "avg_pct_per_trade", 4), ("PF", "pf", 3),
        ("MDD %p(단순)", "mdd_simple_pp", 3), ("MDD %(복리)", "mdd_compound_pct", 3),
        ("수익일", "profit_days", 0), ("손실일", "loss_days", 0),
        ("최대연속손실 건", "max_consec_losses", 0),
        ("연속손실합 %", "max_consec_loss_sum_pct", 3),
        ("Top5 비중 %", "top5_share_pct", 1), ("Top10 비중 %", "top10_share_pct", 1),
        ("Top10제외 단순 %", "ex_top10_simple_pct", 3),
        ("Top10제외 복리 %", "ex_top10_compound_pct", 3),
    ]
    allm = {}
    for pname, pdates in (("FULL 30일", dates), ("TRAIN 20일", train), ("OOS 10일", oos)):
        print("\n" + "=" * 122)
        print(f"[2] {pname}  ({pdates[0]} ~ {pdates[-1]})")
        ms = {nm: metrics(sub(tr, pdates), len(pdates)) for nm, tr in variants}
        allm[pname] = ms
        print(f"{'지표':<20}" + "".join(f"{nm:>16}" for nm, _ in variants))
        for disp, key, nd in rowspec:
            if key is None:
                vals = [f"{ms[nm]['wins']}/{ms[nm]['losses']}" for nm, _ in variants]
                print(f"{disp:<20}" + "".join(f"{v:>16}" for v in vals))
                continue
            print(f"{disp:<20}" + "".join(
                cell(ms[nm].get(key), 16, nd) if isinstance(ms[nm].get(key), float)
                else f"{str(ms[nm].get(key)):>16}" for nm, _ in variants))

    # ── 3) OOS 최우선 판정 ───────────────────────────────────────────────
    print("\n" + "=" * 122)
    print("[3] 최우선 판정 기준 — OOS에서 A보다 복리↑ & PF↑ & MDD 악화 없음")
    oa = allm["OOS 10일"]["A 현행"]
    for nm, _ in variants[1:]:
        m = allm["OOS 10일"][nm]
        c_ok = m["compound_cum_pct"] > oa["compound_cum_pct"]
        p_ok = (m["pf"] or 0) > (oa["pf"] or 0)
        d_ok = m["mdd_compound_pct"] <= oa["mdd_compound_pct"] + 1e-9
        verdict = "통과" if (c_ok and p_ok and d_ok) else "탈락"
        print(f"  {nm:<14} 복리 {m['compound_cum_pct']:>7.3f} vs {oa['compound_cum_pct']:.3f} "
              f"[{'O' if c_ok else 'X'}]   PF {m['pf']:>6.3f} vs {oa['pf']:.3f} "
              f"[{'O' if p_ok else 'X'}]   MDD {m['mdd_compound_pct']:>6.3f} vs "
              f"{oa['mdd_compound_pct']:.3f} [{'O' if d_ok else 'X'}]  →  {verdict}")

    # ── 4) Profit Lock pair table ───────────────────────────────────────
    print("\n" + "=" * 122)
    print(f"[4] Profit Lock({names[best_f]}) 발동 거래 pair table — C는 A와 진입 동일하므로 1:1 대응")
    print("    러너 훼손 = A에서 최종 +3~6% 로 끝난 거래가 lock 때문에 그보다 낮게 끝난 경우")
    assert len(A) == len(C), "A/C 진입 집합 불일치 — pair 불가"
    hdr = (f"{'날짜':<10}{'slot':>5}{'구간':>7}{'방향':>10}{'진입':>7}{'MFE%':>7}"
           f"{'A청산사유':>26}{'A최종%':>9}{'C청산사유':>24}{'C최종%':>9}"
           f"{'순차이%p':>10}{'손실→+':>8}{'러너훼손':>9}")
    print(hdr)
    fired, armed_only = [], []
    for i in range(len(A)):
        if not C[i]["lock_armed"]:
            continue
        (fired if C[i]["lock_fired"] else armed_only).append(i)
    for i in fired:
        ta, tc = A[i], C[i]
        seg = "TRAIN" if ta["date"] in set(train) else "OOS"
        diff = tc["net_pct"] - ta["net_pct"]
        flip = "Y" if (ta["net_pct"] <= 0 and tc["net_pct"] > 0) else "-"
        runner = "Y" if (3.0 <= ta["net_pct"] <= 6.0 and tc["net_pct"] < ta["net_pct"]) else "-"
        print(f"{ta['date']:<10}{str(ta['slot_number']):>5}{seg:>7}{ta['direction']:>10}"
              f"{ta['entry_time'][11:16]:>7}{ta['peak_net_pct']:>7.2f}"
              f"{str(ta['exit_reason'])[:24]:>26}{ta['net_pct']:>9.3f}"
              f"{str(tc['exit_reason'])[:22]:>24}{tc['net_pct']:>9.3f}"
              f"{diff:>+10.3f}{flip:>8}{runner:>9}")
    if fired:
        for seg, ds in (("TRAIN", train), ("OOS", oos), ("FULL", dates)):
            ii = [i for i in fired if A[i]["date"] in set(ds)]
            if not ii:
                print(f"    {seg}: 발동 0건")
                continue
            da = sum(A[i]["net_pct"] for i in ii)
            dc = sum(C[i]["net_pct"] for i in ii)
            print(f"    {seg}: 발동 {len(ii)}건  A합 {da:+.3f}%  C합 {dc:+.3f}%  "
                  f"순차이 {dc-da:+.3f}%p  손실→플러스 전환 "
                  f"{sum(1 for i in ii if A[i]['net_pct']<=0 and C[i]['net_pct']>0)}건  "
                  f"러너(+3~6%) 훼손 "
                  f"{sum(1 for i in ii if 3.0<=A[i]['net_pct']<=6.0 and C[i]['net_pct']<A[i]['net_pct'])}건")
    print(f"\n    lock ARMED(진입CHOP & MFE>=+1.5% 도달)이지만 미발동: {len(armed_only)}건 "
          f"— A와 결과 동일 (lock floor에 닿기 전에 기존 래더가 청산)")
    for i in armed_only:
        ta, tc = A[i], C[i]
        seg = "TRAIN" if ta["date"] in set(train) else "OOS"
        print(f"      {ta['date']} slot{ta['slot_number']} {seg} MFE {ta['peak_net_pct']:.2f}% "
              f"A={ta['net_pct']:+.3f}% ({str(ta['exit_reason'])[:22]})  C={tc['net_pct']:+.3f}%")

    # ── 5) B의 Slot1 TQ4 필터 효과 ──────────────────────────────────────
    print("\n" + "=" * 122)
    print("[5] B: Slot1 Trend Quality >= 4/5 게이트가 실제로 막은 진입")
    ka = {(t["entry_time"], t["entry_symbol"]) for t in B}
    blocked = [t for t in A if (t["entry_time"], t["entry_symbol"]) not in ka]
    print(f"    A 진입 {len(A)}건 → B 진입 {len(B)}건 (차단 {len(blocked)}건)")
    for t in blocked:
        seg = "TRAIN" if t["date"] in set(train) else "OOS"
        print(f"      {t['date']} {seg} slot{t['slot_number']} {t['direction']} "
              f"{t['entry_time'][11:16]} A최종 {t['net_pct']:+.3f}% "
              f"({str(t['exit_reason'])[:24]})")
    if blocked:
        for seg, ds in (("TRAIN", train), ("OOS", oos), ("FULL", dates)):
            bb = [t for t in blocked if t["date"] in set(ds)]
            if bb:
                print(f"    {seg} 차단분 A수익 합계 {sum(t['net_pct'] for t in bb):+.3f}% "
                      f"({len(bb)}건, 승 {sum(1 for t in bb if t['net_pct']>0)}건)")
    added = [t for t in B if (t["entry_time"], t["entry_symbol"])
             not in {(x["entry_time"], x["entry_symbol"]) for x in A}]
    print(f"    B에만 새로 생긴 진입(차단의 연쇄효과): {len(added)}건" +
          ("".join(f"\n      {t['date']} slot{t['slot_number']} {t['direction']} "
                   f"{t['entry_time'][11:16]} {t['net_pct']:+.3f}%" for t in added) if added else ""))

    print("\n" + "=" * 122)
    print("[6] 청산 사유 분포 (FULL 30일)")
    for nm, tr in variants:
        print(f"  {nm}: " + ", ".join(f"{a}={b}" for a, b in
                                      Counter(t["exit_reason"] for t in tr).most_common()))

    (eng.OUTPUT_DIR / "report_summary.json").write_text(json.dumps({
        "period": raw["period"], "chop_cfg": raw["chop_cfg"],
        "lock_floor_selected_on_train": best_f,
        "lock_candidates_train": {f: train_rows[f] for f in train_rows},
        "metrics": allm,
        "c_frozen_consistent": c_consistent,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {eng.OUTPUT_DIR / 'report_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
