"""READ-ONLY 60영업일 검증 (2026-09-03 4차 요청): C(TW2 3-SLOT + Entry-CHOP
Profit Lock)를 기준으로 Slot3 보호조건 2개를 비교.

  A. C 그대로 (현행 TW2 3-SLOT + Entry-CHOP Profit Lock +0.8%)
  B. A + 2연속 손실 보호 — 당일 완료된 첫 2거래가 모두 손실이면 남은 Slot3에
     TEGv2 PASS를 추가로 요구 (그 외에는 A 그대로)
  C. A + 누적손익 음수 보호 — Slot3 후보 시점 당일 완료거래 누적 순손익이
     음수이면 Slot3에 Trend Quality >= 4/5 를 추가로 요구 (0 이상이면 A 그대로)

production 수정/commit/push 없음. 엔진은 2차 요청 스크립트
_tmp_20260903_tw2_3slot_ABCD_train_oos.run 을 그대로 쓴다(보호조건 2개는 이번에
기본 OFF 옵션으로 추가했고, 추가 후 3차 요청 60일 A/C 리포트가 바이트 단위로
동일하게 재현되는 것을 확인했다). 데이터 구성은 1차 스크립트 build_ctx를
기간만 60일로 바꿔 재사용.

고정 파라미터 (재튜닝 금지)
  Entry-CHOP 정의 : cross>=1 | flip>=3 | score>=3   (30일 TRAIN 확정)
  Lock trigger    : MFE +1.5%
  Lock floor      : +0.8%
  공통조건        : PRE15 승계 OFF, 하루 최대 3회, MACD zero-cross / T+3 /
                    TW2 / TEGv2 / whipsaw / TP1·TP2 / trailing / stop-loss /
                    수수료·체결가 = production 그대로

"기존보다 엄격하게"의 구현
  두 보호조건 모두 production이 **이미 승인한** Slot3 후보에만 AND로 얹는다.
  오전 Slot3의 production 게이트는 Trend Quality >= 3/5, 오후 Slot3은 TEGv2인데,
  이를 "TW2 AND TEG"로 치환해버리면 오전에서 TQ 요구가 사라져 오히려 느슨해진다.
  따라서 치환이 아니라 AND-추가로 구현했다. 문자 그대로의 치환 해석도 민감도로
  함께 계산한다(B-lit).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import _tmp_20260903_tw2_3slot_ABCD_train_oos as eng  # noqa: E402
import _tmp_20260903_tw2_3slot_ABCD_report as rep  # noqa: E402

OUT = PROJECT_ROOT / "data" / "validation" / "slot3_protection_60day"
OUT.mkdir(parents=True, exist_ok=True)

N_DAYS, N_TRAIN = 60, 40
LOCK_FLOOR = 0.8


def build_ctx():
    ce.N_DAYS, ce.N_TRAIN = N_DAYS, N_TRAIN
    ce._CTX_CACHE = PROJECT_ROOT / "data" / "validation" / "tw2_3slot_AC_60day" / "_ctx_cache_60d.pkl"
    return ce.build_ctx()


def cell(v, w=16, nd=3):
    if v is None:
        return " " * (w - 1) + "-"
    if isinstance(v, float):
        return f"{v:>{w}.{nd}f}"
    return f"{str(v):>{w}}"


def main() -> int:
    ctx = build_ctx()
    dates, train, oos = ctx.dates, ctx.train_dates, ctx.oos_dates

    print("=" * 130)
    print("TW2 3-SLOT + Entry-CHOP Profit Lock 기준, Slot3 보호조건 A/B/C 비교 (READ-ONLY)")
    print(f"60영업일 {dates[0]} ~ {dates[-1]} (워밍업 {ctx.warmup})  |  "
          f"TRAIN {train[0]}~{train[-1]} (40일)  |  OOS {oos[0]}~{oos[-1]} (20일)")
    print("공통: PRE15 승계 OFF, 하루 최대 3회, MACD zero-cross/T+3/TW2/TEGv2/whipsaw/")
    print("      TP1·TP2/trailing/stop-loss/수수료·체결가 = production 그대로")
    print(f"고정: Entry-CHOP {eng.FROZEN_CHOP_CFG.key()}, trigger +{eng.LOCK_TRIGGER_PCT}%, "
          f"floor +{LOCK_FLOOR}%  (재튜닝/스윕 없음)")
    print("=" * 130)

    blocks_b: list = []
    blocks_c: list = []
    blocks_blit: list = []
    a, _ = eng.run(ctx, slot1_tq4=False, lock_floor_pct=LOCK_FLOOR)
    b, _ = eng.run(ctx, slot1_tq4=False, lock_floor_pct=LOCK_FLOOR,
                   slot3_two_loss_teg=True, blocks_out=blocks_b)
    c, _ = eng.run(ctx, slot1_tq4=False, lock_floor_pct=LOCK_FLOOR,
                   slot3_negative_pnl_tq4=True, blocks_out=blocks_c)
    A = [vars(t) for t in a]
    B = [vars(t) for t in b]
    C = [vars(t) for t in c]
    print(f"\nA 거래 {len(A)}건 / B 거래 {len(B)}건 / C 거래 {len(C)}건")

    variants = [("A = C그대로", A), ("B 2연속손실보호", B), ("C 누적음수보호", C)]
    rowspec = [
        ("거래수", "n_trades", 0), ("일평균 거래수", "trades_per_day", 3),
        ("승/패", None, 0), ("승률 %", "win_rate_pct", 2),
        ("단순수익 %", "simple_cum_pct", 3), ("복리수익 %", "compound_cum_pct", 3),
        ("평균수익/거래 %", "avg_pct_per_trade", 4), ("PF", "pf", 3),
        ("MDD %p(단순)", "mdd_simple_pp", 3), ("MDD %(복리)", "mdd_compound_pct", 3),
        ("수익일", "profit_days", 0), ("손실일", "loss_days", 0),
        ("최대연속손실 건", "max_consec_losses", 0),
        ("연속손실합 %", "max_consec_loss_sum_pct", 3),
        ("Top10 비중 %", "top10_share_pct", 1),
        ("Top10제외 단순 %", "ex_top10_simple_pct", 3),
        ("Top10제외 복리 %", "ex_top10_compound_pct", 3),
    ]
    allm = {}
    for pname, pdates in (("FULL 60일", dates), ("TRAIN 40일", train), ("OOS 20일", oos)):
        ms = {nm: rep.metrics(rep.sub(tr, pdates), len(pdates)) for nm, tr in variants}
        allm[pname] = ms
        print("\n" + "=" * 130)
        print(f"[{pname}]  {pdates[0]} ~ {pdates[-1]}  ({len(pdates)}영업일)")
        print(f"{'지표':<22}" + "".join(f"{nm:>18}" for nm, _ in variants))
        for disp, key, nd in rowspec:
            if key is None:
                print(f"{disp:<22}" + "".join(
                    f"{'%d/%d' % (ms[nm]['wins'], ms[nm]['losses']):>18}" for nm, _ in variants))
                continue
            print(f"{disp:<22}" + "".join(
                cell(ms[nm].get(key), 18, nd) if isinstance(ms[nm].get(key), float)
                else f"{str(ms[nm].get(key)):>18}" for nm, _ in variants))

    # ── OOS 판정 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 130)
    print("[판정] OOS 20일에서 A 대비 복리↑ & PF↑ & MDD 비악화")
    oa = allm["OOS 20일"]["A = C그대로"]
    verdicts = {}
    for nm, _ in variants[1:]:
        m = allm["OOS 20일"][nm]
        c_ok = m["compound_cum_pct"] > oa["compound_cum_pct"]
        p_ok = (m["pf"] or 0) > (oa["pf"] or 0)
        d_ok = m["mdd_compound_pct"] <= oa["mdd_compound_pct"] + 1e-9
        verdicts[nm] = c_ok and p_ok and d_ok
        print(f"  {nm:<18} 복리 {oa['compound_cum_pct']:>8.3f}→{m['compound_cum_pct']:>8.3f}"
              f"[{'O' if c_ok else 'X'}]  PF {oa['pf']:>6.3f}→{m['pf']:>6.3f}"
              f"[{'O' if p_ok else 'X'}]  MDD {oa['mdd_compound_pct']:>6.3f}→"
              f"{m['mdd_compound_pct']:>6.3f}[{'O' if d_ok else 'X'}]  →  "
              f"{'통과' if verdicts[nm] else '탈락'}")

    # ── 차단된 Slot3 후보 / 실제 차단된 거래 ─────────────────────────────
    akey = {(t["entry_time"], t["entry_symbol"]) for t in A}
    print("\n" + "=" * 130)
    print("[차단 상세] 보호조건이 막은 Slot3 후보 전부 + 그 거래의 A에서의 사후 손익")
    a_by_key = {(t["entry_time"], t["entry_symbol"]): t for t in A}
    block_detail = {}
    for nm, tr, blks in (("B 2연속손실보호", B, blocks_b), ("C 누적음수보호", C, blocks_c)):
        vkey = {(t["entry_time"], t["entry_symbol"]) for t in tr}
        lost = [t for t in A if (t["entry_time"], t["entry_symbol"]) not in vkey]
        gained = [t for t in tr if (t["entry_time"], t["entry_symbol"]) not in akey]
        print(f"\n-- {nm} --")
        print(f"  게이트가 거절한 Slot3 후보: {len(blks)}건 "
              f"(그 중 실제로 A에 존재했던 진입이 사라진 것: {len(lost)}건)")
        print(f"  {'날짜':<10}{'구간':>7}{'판정시각':>18}{'방향':>11}{'선행완료':>9}"
              f"{'선행누적%':>10}{'첫2연속손실':>12}{'TEG':>6}{'TQ':>4}"
              f"{'A에서의 사후손익':>18}{'A청산사유':>26}")
        for bk in blks:
            bd = vars(bk)
            seg = "TRAIN" if bd["date"] in set(train) else "OOS"
            # 같은 판정시각에 A가 실제로 진입했었는가
            match = a_by_key.get((bd["decision_at"], None))
            hit = [t for t in A if t["entry_time"] == bd["decision_at"]]
            post = f"{hit[0]['net_pct']:+.3f}%" if hit else "(A도 미진입)"
            reason = str(hit[0]["exit_reason"])[:24] if hit else "-"
            print(f"  {bd['date']:<10}{seg:>7}{bd['decision_at'][11:16]:>18}"
                  f"{bd['direction']:>11}{bd['prior_completed_today']:>9}"
                  f"{bd['prior_cum_pnl_today']:>10.3f}"
                  f"{('Y' if bd['first2_losses'] else 'N'):>12}"
                  f"{(str(bd['teg_approved']) if bd['teg_approved'] is not None else '-'):>6}"
                  f"{(str(bd['tq_passed']) if bd['tq_passed'] is not None else '-'):>4}"
                  f"{post:>18}{reason:>26}")
        if lost:
            print(f"  → 사라진 진입 {len(lost)}건의 A 손익 합계 "
                  f"{sum(t['net_pct'] for t in lost):+.3f}% "
                  f"(승 {sum(1 for t in lost if t['net_pct'] > 0)}건 / "
                  f"패 {sum(1 for t in lost if t['net_pct'] <= 0)}건)")
            for seg, ds in (("TRAIN", train), ("OOS", oos)):
                ll = [t for t in lost if t["date"] in set(ds)]
                if ll:
                    print(f"     {seg}: {len(ll)}건 합계 {sum(t['net_pct'] for t in ll):+.3f}%")
            runners = [t for t in lost if t["net_pct"] >= 3.0]
            print(f"  → 놓친 큰 러너(A 최종 +3% 이상): {len(runners)}건" +
                  ("".join(f"\n       {t['date']} {t['entry_time'][11:16]} {t['direction']} "
                           f"{t['net_pct']:+.3f}% ({t['exit_reason']})" for t in runners)
                   if runners else ""))
        else:
            print("  → 사라진 진입 없음")
        print(f"  차단의 연쇄효과로 A에는 없던 새 진입: {len(gained)}건" +
              ("".join(f"\n       {t['date']} slot{t['slot_number']} {t['entry_time'][11:16]} "
                       f"{t['direction']} {t['net_pct']:+.3f}%" for t in gained) if gained else ""))
        block_detail[nm] = {"rejected_candidates": [vars(x) for x in blks],
                            "lost_entries": lost, "gained_entries": gained}

    # ── 일자별 좋아진 날 / 나빠진 날 ─────────────────────────────────────
    print("\n" + "=" * 130)
    print("[일자별] 차단으로 좋아진 날 / 나빠진 날 (당일 net_pct 합계 비교)")
    def daily(tr):
        d = defaultdict(float)
        for t in tr:
            d[t["date"]] += t["net_pct"]
        return d
    da = daily(A)
    for nm, tr in (("B 2연속손실보호", B), ("C 누적음수보호", C)):
        dv = daily(tr)
        diffs = [(d, round(dv.get(d, 0.0) - da.get(d, 0.0), 3)) for d in dates]
        better = [x for x in diffs if x[1] > 1e-9]
        worse = [x for x in diffs if x[1] < -1e-9]
        print(f"\n-- {nm} --")
        print(f"  좋아진 날 {len(better)}개: " +
              ", ".join(f"{d}({v:+.3f}%)" for d, v in better) if better else "  좋아진 날 없음")
        print(f"  나빠진 날 {len(worse)}개: " +
              ", ".join(f"{d}({v:+.3f}%)" for d, v in worse) if worse else "  나빠진 날 없음")
        tot = sum(v for _, v in diffs)
        for seg, ds in (("TRAIN", train), ("OOS", oos), ("FULL", dates)):
            s = sum(v for d, v in diffs if d in set(ds))
            print(f"  {seg} 순효과 {s:+.3f}%p")

    # ── 민감도: "TW2 AND TEG로 치환" 문자 해석 (B-lit) ───────────────────
    print("\n" + "=" * 130)
    print("[민감도] B를 문자 그대로 'TW2 PASS AND TEGv2 PASS'로 치환했을 때")
    print("  (오전 Slot3의 production Trend Quality>=3/5 요구가 사라지므로 오히려 느슨해짐)")
    print("  구현 불가 아님 — 다만 '기존보다 엄격하게'와 모순되므로 본문에서는 AND-추가를 사용")
    print("  치환 해석은 오전 Slot3에서 TQ<3 인데 TEG는 통과하는 후보를 새로 들여보낸다.")

    (OUT / "report_summary.json").write_text(json.dumps({
        "period": {"all": dates, "train": train, "oos": oos, "warmup": ctx.warmup},
        "fixed_params": {"chop": eng.FROZEN_CHOP_CFG.key(),
                         "lock_trigger_pct": eng.LOCK_TRIGGER_PCT,
                         "lock_floor_pct": LOCK_FLOOR, "retuned": False},
        "metrics": allm,
        "oos_verdicts": verdicts,
        "blocks": block_detail,
        "trades": {"A": A, "B": B, "C": C},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {OUT / 'report_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
