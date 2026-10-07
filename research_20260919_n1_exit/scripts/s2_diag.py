"""S2 — N1 158거래를 진입시점 정보로 쪼개 손실이 어디 몰려 있는지 진단.
전부 사후 라벨이 아니라 **진입 판정시점에 이미 알 수 있던 값**만 쓴다."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd, numpy as np
from pathlib import Path
HERE = Path(__file__).resolve().parent
df = pd.read_csv(HERE / "RESULTS" / "trades_N1.csv")
df["pnl"] = df.pnl_w1a
print(f"N1 78일 {len(df)}거래  단순합 {df.pnl.sum():+.2f}  "
      f"승 {int((df.pnl>0).sum())} / 패 {int((df.pnl<0).sum())}\n")

def split(name, mask):
    T, F = df[mask], df[~mask]
    def s(x):
        if not len(x):
            return " " * 46
        eq = (1 + x.sort_values("exit_time").pnl / 100).cumprod().iloc[-1]
        return (f"n={len(x):3d} 합={x.pnl.sum():+7.2f} 평균={x.pnl.mean():+6.3f} "
                f"승률={100*(x.pnl>0).mean():4.1f} 복리={100*(eq-1):+7.2f}")
    print(f"{name:26s} T: {s(T)}\n{'':26s} F: {s(F)}")

print("── 진입시점 라벨별 ──")
split("entry_chop (CHOP)", df.entry_chop.astype(bool))
for k in (1, 2, 3):
    split(f"chop_score >= {k}", df.chop_score >= k)
split("trend_at_entry (EMA20/50 OK)", df.trend_at_entry.astype(bool))
split("session MORNING", df.session == "MORNING")
for s_ in (1, 2, 3):
    split(f"slot {s_}", df.slot_number == s_)
split("tq 있음(slot2 판정거래)", df.tq.notna())
for q in (4, 5, 6):
    split(f"tq >= {q} (tq 있는 거래만)", df.tq.fillna(99) >= q)
split("w1a < 1.0 (사이징 축소)", df.w1a < 1.0)
df["hh"] = pd.to_datetime(df.entry_time).dt.hour * 60 + pd.to_datetime(df.entry_time).dt.minute
for t_, lab in ((9 * 60 + 30, "09:30"), (10 * 60, "10:00"), (11 * 60, "11:00")):
    split(f"진입 < {lab}", df.hh < t_)

print("\n── 손실거래(STOP_LOSS) 구성 ──")
sl = df[df.exit_reason == "TIME_WINDOW_STOP_LOSS"]
print(f"  STOP_LOSS {len(sl)}건 합 {sl.pnl.sum():+.2f} (전체 손실 "
      f"{df[df.pnl<0].pnl.sum():+.2f} 의 {100*sl.pnl.sum()/df[df.pnl<0].pnl.sum():.0f}%)")
for c in ("entry_chop", "trend_at_entry"):
    print(f"    {c}=True {int(sl[c].astype(bool).sum()):2d}/{len(sl)}  "
          f"(전체에서는 {int(df[c].astype(bool).sum())}/{len(df)})")
print(f"    slot 분포 {dict(sl.slot_number.value_counts().sort_index())}  "
      f"(전체 {dict(df.slot_number.value_counts().sort_index())})")
print(f"    session {dict(sl.session.value_counts())}  (전체 {dict(df.session.value_counts())})")

print("\n── 일별 집중도 ──")
d = df.groupby("date").pnl.sum().sort_values()
print("  최악 5일:", ", ".join(f"{k} {v:+.2f}" for k, v in d.head(5).items()))
print("  최고 5일:", ", ".join(f"{k} {v:+.2f}" for k, v in d.tail(5).items()))
print(f"  손실일 {int((d<0).sum())}/{len(d)}일, 손실일 합 {d[d<0].sum():+.2f}")
mult = df.groupby("date").size()
print(f"  하루 진입수별: " + ", ".join(
    f"{k}건날({v}일) 합 {df[df.date.isin(mult[mult==k].index)].pnl.sum():+.2f}"
    for k, v in mult.value_counts().sort_index().items()))
