"""A1 — N1 거래 분포: MFE(peak) 대비 실현손익. runner vs 반납 유형 규모 파악.
READ-ONLY, 기존 산출 CSV 만 읽는다.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
from app.trading.macd2 import config, time_window_3slot as tw3

print("=== X2-lite exit_overrides (production) ===")
for k, v in tw3.exit_overrides(tw3.MODE_X2LITE_3SLOT).items():
    print(f"  {k:36s} {v}")
print("  morning_tp2_pct_override            ", tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_3SLOT))
print("\nX2LITE ExitParams:", H.X2LITE)
print("\n참고 상수:")
for n in ("MORNING_TP1", "MORNING_TP2", "TW2_MORNING_TP2", "MORNING_TRAILING_TRIGGER",
          "MORNING_TRAILING_STOP", "MORNING_AFTER_TP1_STOP", "MORNING_STOP_LOSS",
          "AFTERNOON_TP", "EARLY_TP_TRIGGER_PCT", "EARLY_TP_FLOOR_PCT",
          "H50_TREND_EMA_FAST", "H50_TREND_EMA_SLOW", "QUALITY_SCORE_THRESHOLD"):
    print(f"  config.{n:28s} {getattr(config, n, '(none)')}")

for win, f in (("78일", "d78_trades_N1.csv"), ("30일", "d30_trades_N1.csv")):
    df = pd.read_csv(HERE / f)
    df = df[df.session == "MORNING"] if False else df
    print(f"\n{'='*86}\n{win}  N1  n={len(df)}   (세션별: "
          + ", ".join(f"{k}={v}" for k, v in df.session.value_counts().items()) + ")")
    print(f"\nexit_reason 분포:")
    for r, c in df.exit_reason.value_counts().items():
        sub = df[df.exit_reason == r]
        print(f"  {r:34s} {c:4d}  평균net={sub.net_pct.mean():+7.3f}  평균peak={sub.peak_net_pct.mean():6.3f}")

    print(f"\nMFE(peak) 버킷 × 결과 — 반납 구조")
    bins = [-99, 1.0, 2.0, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 999]
    lab = ["<1", "1-2", "2-3", "3-3.5", "3.5-4", "4-5", "5-6", "6-8", ">=8"]
    df["pk"] = pd.cut(df.peak_net_pct, bins=bins, labels=lab)
    g = df.groupby("pk", observed=True).agg(n=("net_pct", "size"), net=("net_pct", "mean"),
                                            pk_m=("peak_net_pct", "mean"))
    g["반납"] = (g.pk_m - g.net).round(3)
    print(g.round(3).to_string())

    # 핵심 질문: peak>=3 인 거래 중 8% 도달(runner) vs 반납
    hi = df[df.peak_net_pct >= 3.0].copy()
    run8 = hi[hi.peak_net_pct >= 8.0]
    mid = hi[(hi.peak_net_pct >= 3.0) & (hi.peak_net_pct < 8.0)]
    print(f"\npeak>=3.0 : {len(hi)}건  평균net={hi.net_pct.mean():+.3f}")
    print(f"  그 중 peak>=8(TP2 도달) : {len(run8)}건 평균net={run8.net_pct.mean():+.3f}")
    print(f"  peak 3~8 (미도달)      : {len(mid)}건 평균net={mid.net_pct.mean():+.3f} "
          f"평균peak={mid.peak_net_pct.mean():.3f} 평균반납={(mid.peak_net_pct-mid.net_pct).mean():.3f}")
    print(f"  미도달 중 net<3.0 (3% 밑으로 반납) : {(mid.net_pct < 3.0).sum()}건, "
          f"그 손실합={(mid[mid.net_pct<3.0].peak_net_pct - mid[mid.net_pct<3.0].net_pct).sum():.2f}%p")
    print(f"  미도달 중 net<0   (이익→손실)     : {(mid.net_pct < 0).sum()}건")

    print(f"\npeak>=3.0 거래 전량 (date, session, peak, net, 반납, reason):")
    for _, r in hi.sort_values("peak_net_pct", ascending=False).iterrows():
        print(f"  {r.date} {str(r.session)[:4]:4s} {str(r.entry_time)[11:16]} "
              f"peak={r.peak_net_pct:6.2f} net={r.net_pct:+7.3f} "
              f"반납={r.peak_net_pct - r.net_pct:6.2f}  {r.exit_reason}")
