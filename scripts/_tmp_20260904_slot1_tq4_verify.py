"""READ-ONLY 재검증 (2026-09-04): "하루 3회 유지" 조건에서 현행보다 나은
필터가 있는가 — 유력 후보인 **Slot1 Trend Quality >= 4/5** 를 오늘(20260904)
데이터를 포함한 창에서 다시 확인한다.

  A(현행) = TW2 3-SLOT + 조기익절, 하루 최대 3회
  C       = A + Slot1(그날 첫 신규진입)에만 Trend Quality >= 4/5 AND-게이트
            (미달이면 진입하지 않고 슬롯 미소비 -> 이후 후보가 다시 Slot1 조건)

기존 abc_30day_compare 는 20260721~20260903 창이라 오늘이 빠져 있었다. 여기서는
연구 3에서 만든 60일 ctx(20260609~20260904, 오늘 포함)로 A/C 를 full-chain 재생하고
30일창(20260722~20260904) / 60일창 양쪽 지표를 낸다.

체인은 검증된 _tmp_20260904_abc_30day_compare.run() 을 그대로 호출한다
(production 함수만 사용, 무수정). 하루 최대 3회는 resolve_slot 의
TW2_3SLOT_DAILY_CAP 그대로이며 C 는 진입 수를 늘리지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _tmp_20260904_abc_30day_compare as abc  # noqa: E402
import _tmp_20260904_runner_dataset as ds  # noqa: E402

OUT = PROJECT_ROOT / "data" / "validation" / "slot1_tq4_verify"
OUT.mkdir(parents=True, exist_ok=True)


def metrics(trades, dates) -> dict:
    ts = [t for t in trades if t["date"] in set(dates)]
    if not ts:
        return {}
    df = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    w = df[df["net_pct"] > 0]
    l = df[df["net_pct"] < 0]
    eq = (1 + df["net_pct"] / 100).cumprod()
    gp, gl = w["net_pct"].sum(), -l["net_pct"].sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    top10 = df.nlargest(min(10, n), "net_pct")
    rest = df.drop(top10.index)
    t10c = (((1 + rest.sort_values("exit_time")["net_pct"] / 100).cumprod().iloc[-1] - 1) * 100
            if len(rest) else 0.0)
    return {
        "trades": n, "avg_per_day": round(n / len(dates), 3),
        "wins": int(len(w)), "losses": int(len(l)),
        "win_rate_pct": round(len(w) / n * 100, 2),
        "simple_pct": round(float(df["net_pct"].sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df["net_pct"].mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "top10_excl_simple_pct": round(float(rest["net_pct"].sum()), 4),
        "top10_excl_compound_pct": round(float(t10c), 4),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
        "trades_ge_5pct": int((df["net_pct"] >= 5.0).sum()),
    }


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    d60 = ctx.dates
    d30 = d60[-30:]
    print(f"ctx {d60[0]}~{d60[-1]} (60일) / 30일창 {d30[0]}~{d30[-1]}")

    blocks: list = []
    trades_a, dec_a = abc.run(ctx, slot1_tq4=False, early_tp_enabled=True,
                              record_decisions=True)
    trades_c, dec_c = abc.run(ctx, slot1_tq4=True, early_tp_enabled=True,
                              record_decisions=True, blocks_out=blocks)
    A = [vars(t) for t in trades_a]
    C = [vars(t) for t in trades_c]

    # A 재현 검증 (30일창이 직전 검증엔진과 동일한지)
    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))["A"]
        k = lambda t: (t["date"], t["entry_time"], t["direction"],
                       round(t["net_pct"], 6), t["exit_reason"])
        o = {k(t) for t in old}
        n = {k(t) for t in A if t["date"] in set(d30)}
        print(f"A 재현 검증(30일창): {o == n} [old {len(o)} / new {len(n)}]")

    res = {}
    for scope, dd in (("30일", d30), ("60일", d60)):
        res[scope] = {"A": metrics(A, dd), "C": metrics(C, dd)}
        a, c = res[scope]["A"], res[scope]["C"]
        print(f"\n== {scope} ({dd[0]}~{dd[-1]}) ==")
        print(f"{'지표':<22}{'A 현행':>14}{'C Slot1TQ>=4':>16}{'차이':>12}")
        for lab, key, f in (("거래수", "trades", "{:.0f}"),
                            ("승률(%)", "win_rate_pct", "{:.2f}"),
                            ("단순(%)", "simple_pct", "{:+.3f}"),
                            ("복리(%)", "compound_pct", "{:+.3f}"),
                            ("평균/거래(%)", "avg_pct", "{:+.4f}"),
                            ("PF", "pf", "{:.4f}"),
                            ("MDD(%)", "mdd_pct", "{:.3f}"),
                            ("Top10제외 단순(%)", "top10_excl_simple_pct", "{:+.3f}"),
                            ("Top10제외 복리(%)", "top10_excl_compound_pct", "{:+.3f}"),
                            ("수익일", "profit_days", "{:.0f}"),
                            ("손실일", "loss_days", "{:.0f}"),
                            ("+3% 거래", "trades_ge_3pct", "{:.0f}")):
            av, cv = a.get(key), c.get(key)
            diff = (f"{cv - av:+.4f}" if isinstance(av, (int, float))
                    and isinstance(cv, (int, float)) else "")
            print(f"{lab:<22}{f.format(av):>14}{f.format(cv):>16}{diff:>12}")

    # 차단된 Slot1 후보와 A 기준 사후손익
    print(f"\n== Slot1 TQ<4 로 차단된 후보 {len(blocks)}건 ==")
    rows = []
    for b in blocks:
        bd = vars(b)
        a_t = next((t for t in A if t["decision_idx"] == bd["decision_idx"]
                    and t["direction"] == bd["direction"]), None)
        scope = "30일창" if bd["date"] in set(d30) else "60일전용"
        rows.append({**bd, "A_net_pct": a_t["net_pct"] if a_t else None,
                     "A_peak": a_t["peak_net_pct"] if a_t else None,
                     "A_exit": a_t["exit_reason"] if a_t else None, "scope": scope})
        print(f"  [{scope}] {bd['date']} {bd['decision_at'][11:16]} {bd['direction']:<10} "
              f"TQ {bd['tq_passed']}/5 -> A실현 "
              + (f"{a_t['net_pct']:+.3f}% (MFE {a_t['peak_net_pct']:+.2f}%, {a_t['exit_reason']})"
                 if a_t else "A에도 진입 없음"))
    for scope, dd in (("30일", d30), ("60일", d60)):
        sub = [r for r in rows if r["date"] in set(dd) and r["A_net_pct"] is not None]
        if sub:
            print(f"  {scope} 차단분 A기준 합계 {sum(r['A_net_pct'] for r in sub):+.3f}% "
                  f"(승 {sum(1 for r in sub if r['A_net_pct'] > 0)} / "
                  f"패 {sum(1 for r in sub if r['A_net_pct'] < 0)}), n={len(sub)}")

    # 차단 후 새로 생긴 거래
    akeys = {(t["decision_idx"], t["direction"]) for t in A}
    new = [t for t in C if (t["decision_idx"], t["direction"]) not in akeys]
    print(f"\n== 차단으로 슬롯이 남아 새로 진입한 거래 {len(new)}건 ==")
    for t in new:
        scope = "30일창" if t["date"] in set(d30) else "60일전용"
        print(f"  [{scope}] {t['date']} {t['entry_time'][11:16]}->{(t['exit_time'] or '')[11:16]} "
              f"Slot{t['slot_number']} {t['direction']:<10} {t['net_pct']:+.3f}% "
              f"({t['exit_reason']})")

    (OUT / "result.json").write_text(json.dumps({
        "dates30": d30, "dates60": d60, "metrics": res,
        "slot1_blocks": rows, "new_entries": new,
        "A": A, "C": C,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {OUT / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
