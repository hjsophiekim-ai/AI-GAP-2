"""오후 XS VWAP 근접 override — **적용 후보 전수 사전조사** (재생 불필요).

기존 재생의 게이트 기록(gates)에서 "VWAP 사유만으로 거절된 오후 후보"를 전부 찾고,
각 건에 대해 판정시점의 VWAP 불리폭 / MU ENTRY_CONFIRM / 체결강도를 다시 계산한다.
production 함수를 그대로 쓰고 판정시점까지의 완성봉만 본다(미래 데이터 금지).

    python .../c1_vwapcand.py --tag CHOPX_A

override 정의 (사용자 사전등록):
  · 오후(판정시각 >= TW2_3SLOT_MORNING_WINDOW_END) 후보
  · MU ENTRY_CONFIRM (= MU CONFIRM ∧ HY gap EXPAND)
  · 거절 사유가 **VWAP 하나뿐**
  · VWAP 불리폭 <= 0.10% (비교용 0.20%)
  · 체결강도는 hard veto 에서 제외 — 값은 기록만 한다
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import a3_peaks as P  # noqa: E402
from app.trading.macd2 import config, exec_strength as XS, major_flag_filter as MFF  # noqa: E402
from app.trading.macd2 import mu_assist as MU, signal_engine as SE, teg_gate as TEG  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))
KST = config.KST
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
#: VWAP 계열 거절 사유 (TW2 추가 veto / TEGv2 조건)
VWAP_REASONS = {config.TW2_REJECT_VWAP_VETO, TEG.COND_VWAP}


_CTTR = {}


def _mean_cttr_full(now):
    """exec_strength.mean_cttr 와 **같은 정의**([now-60s, now) 평균). 과거 시각 조회를 위해
    파일 꼬리가 아니라 전체를 읽는다(판정 정의는 바뀌지 않는다)."""
    day = XS._naive_kst(now).strftime("%Y-%m-%d")
    if day not in _CTTR:
        f = RT / "snapshots" / day / "000660.csv"
        if not f.exists():
            _CTTR[day] = None
        else:
            df = pd.read_csv(f, usecols=["ts", "cttr"])
            df["ts"] = pd.to_datetime(df["ts"])
            _CTTR[day] = df.dropna(subset=["cttr"]).set_index("ts")["cttr"].sort_index()
    s = _CTTR[day]
    if s is None or s.empty:
        return None
    end = XS._naive_kst(now)
    w = s[(s.index >= end - pd.Timedelta(seconds=XS.WINDOW_SEC)) & (s.index < end)]
    return float(w.mean()) if len(w) else None


def hy_frame(days):
    fr = []
    for d in days:
        p = SP / "data" / f"replay_{d}_hynix_1m.csv"
        if p.exists():
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            fr.append(x)
    hy = pd.concat(fr).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    return hy.sort_values("datetime").reset_index(drop=True)


def vwap_dev(bars, dirn):
    """보유/후보 방향 기준 VWAP 편차(%). 음수면 불리한 쪽."""
    work = MFF._prepare_bars(bars)
    if work is None or not len(work):
        return None
    vw = MFF._session_vwap(work)
    if not len(vw) or pd.isna(vw.iloc[-1]) or float(vw.iloc[-1]) <= 0:
        return None
    v = float(vw.iloc[-1])
    c = float(work["close"].iloc[-1])
    sgn = 1.0 if dirn == Direction.UP_RED else -1.0
    return (c / v - 1.0) * 100.0 * sgn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    comp = sorted({f.stem.split("_")[1] for f in (SP / "data").glob("replay_*_hynix_1m.csv")})
    cutoff = config.TW2_3SLOT_MORNING_WINDOW_END

    print(f"\n{'='*140}")
    print(f"[{a.tag}] VWAP 사유로 거절된 후보 전수  (오후 기준 = 판정시각 >= {cutoff})")
    print(f"{'='*140}")
    print(f"{'날짜':8s}{'판정':9s}{'방향':10s}{'세션':6s}{'거절사유':34s}{'VWAP편차%':>10s}"
          f"{'MU진입판정':14s}{'cttr':>8s}{'일치':>6s}{'<=0.10':>7s}{'<=0.20':>7s}")
    n_all = n10 = n20 = 0
    rows = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        g = [x for x in j.get("gates", []) if x["t"][:10].replace("-", "") == d]
        i = comp.index(d)
        hy = hy_frame([comp[i - 2], comp[i - 1], d])
        # 그 날의 TEG / TW2VETO 거절을 시각별로 모은다
        for x in g:
            reasons = None
            if x["ev"] == "TEG" and not x.get("approved"):
                reasons = set(x.get("reasons") or [])
            elif x["ev"] == "TW2VETO" and x.get("vetoed"):
                reasons = {x.get("reason")}
            if not reasons:
                continue
            if not (reasons & VWAP_REASONS):
                continue
            n_all += 1
            only_vwap = reasons <= VWAP_REASONS
            t = datetime.fromisoformat(x["t"])
            sess = "오후" if t.timetz().replace(tzinfo=None) >= cutoff else "오전"
            dirn = Direction.UP_RED if x.get("dir") == "UP_RED" else Direction.DOWN_BLUE
            df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
            bars = SE.resample_completed_3m(df1, now=t)
            dev = vwap_dev(bars, dirn)
            # MU ENTRY_CONFIRM (롤링, 앵커 now-5분)
            try:
                ser = MU.load_mu_1m(t, str(RT))
                if ser is not None:
                    ser = ser[ser.index + pd.Timedelta(minutes=1) <= MU._naive(t)]
                anchor = t.replace(second=0, microsecond=0) - pd.Timedelta(minutes=5).to_pytimedelta()
                pat = str(MU.evaluate_pattern(ser, dirn, anchor).get("mu_pattern_state"))
            except Exception:
                pat = "UNAVAILABLE"
            cttr = _mean_cttr_full(t)
            cst = XS.state_for(dirn, cttr)
            ok10 = bool(only_vwap and sess == "오후" and pat == MU.PATTERN_CONFIRM
                        and dev is not None and -0.10 <= dev < 0)
            ok20 = bool(only_vwap and sess == "오후" and pat == MU.PATTERN_CONFIRM
                        and dev is not None and -0.20 <= dev < 0)
            n10 += ok10
            n20 += ok20
            print(f"{d[4:6]+'/'+d[6:]:8s}{x['t'][11:19]:9s}{str(x.get('dir')):10s}{sess:6s}"
                  f"{(','.join(sorted(reasons)))[:34]:34s}{(dev if dev is not None else float('nan')):>10.3f}"
                  f"{pat:14s}{(cttr if cttr is not None else float('nan')):>8.1f}"
                  f"{('O' if ok10 else '.'):>7s}{('O' if ok20 else '.'):>7s}")
            rows.append((d, x["t"][11:19], x.get("dir"), sess, sorted(reasons), dev, pat, cttr, ok10, ok20))
    print(f"\nVWAP 사유 포함 거절 {n_all}건 · override 적용대상: <=0.10% **{n10}건** / <=0.20% **{n20}건**")
    print("주: 'VWAP편차%' 는 후보 방향 기준. 음수면 불리한 쪽이고, override 는 그 절대값이")
    print("    0.10%(비교 0.20%) 이내인 '근접' 상태만 대상으로 한다.")
    print("    체결강도(cttr)는 hard veto 에서 제외하므로 판정에 쓰지 않고 기록만 한다.")


if __name__ == "__main__":
    main()
