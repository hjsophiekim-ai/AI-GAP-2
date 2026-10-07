"""G0 - SHADOW-BASE detector 사전계산 + EXECUTION detector 대조.

BASE 거래열은 결정론적으로 확정돼 있으므로(A_BASE), regime 을 그 청산이력의
**시간 함수**로 미리 계산한다. 대응전략이 거래를 바꿔도 detector 입력이 변하지
않으므로 feedback loop 가 원천적으로 없다.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "c8.pkl", "rb"))
B = pd.DataFrame(Z["runs"]["A_BASE"]).sort_values("exit_time").reset_index(drop=True)
B["ext"] = pd.to_datetime(B.exit_time, utc=True)
h50 = B.h50_held.astype(float).values
tp1 = B.tp1_hit.astype(float).values
ext = B.ext.values
K, HTH, QTH = 10, 0.40, 0.20


def regime_at(ts):
    """시각 ts 이전에 **BASE 에서** 청산된 마지막 K 건으로 판정."""
    n = int(np.searchsorted(ext, np.datetime64(pd.Timestamp(ts).tz_convert("UTC")), side="left"))
    if n < K:
        return False, np.nan, np.nan
    h = h50[n - K:n].mean()
    q = tp1[n - K:n].mean()
    return bool(h >= HTH and q <= QTH), round(float(h), 4), round(float(q), 4)


if __name__ == "__main__":
    import hengine5 as H
    H._CTX_CACHE = HERE / "_ctx_B.pkl"
    ctx = H.build_ctx(78)
    bars = ctx.hynix_bars_3m
    rec = pd.to_datetime(bars["datetime"]) + pd.Timedelta(minutes=3)
    rec = rec.dt.tz_convert("UTC") if rec.dt.tz is not None else rec.dt.tz_localize("Asia/Seoul").dt.tz_convert("UTC")
    on = np.zeros(len(bars), bool)
    hr = np.full(len(bars), np.nan); qr = np.full(len(bars), np.nan)
    for i in range(len(bars)):
        o, h, q = regime_at(rec.iloc[i])
        on[i], hr[i], qr[i] = o, h, q
    date = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
    np.save(HERE / "shadow_on.npy", on)
    print("SHADOW regime ON 봉 %d / %d (%.1f%%)" % (on.sum(), len(on), 100 * on.mean()))
    df = pd.DataFrame({"date": date, "on": on})
    t = df.groupby("date").on.mean()
    sep = [d for d in t.index if d >= "20260901"]
    print("9월 ON 비율 %.1f%% · 비9월 ON 비율 %.1f%%"
          % (100 * t[sep].mean(), 100 * t[[d for d in t.index if d < "20260901"]].mean()))
    print("\n일별 ON 비율 (0 아닌 날만)")
    print(t[t > 0].round(3).to_string())
    # EXECUTION detector 대조 (BASE 실행열 = 같은 열이므로 동일해야 한다)
    print("\n[대조] EXECUTION detector(=BASE 실행열)와 SHADOW 는 BASE 런에서 정의상 동일하다.")
