"""초기 30분 손절 2봉 확인(규칙 B) — 운영구성 58일 손절 전수 반사실 (2026-11-05).

입력
  · 재생 원장  C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out/L85_A_<day>.json
                (= 현행 운영구성 CHOP + MU Assist + XS, 85일 중 1분봉이 있는 58일)
  · ETF 1분봉  data/cache | research_20261002_sept_strategy_replay/data | research_20261020_h50_hold/data
  · MU 플래그  research_20261104_mu_primary/out/mu_flags.csv (10/19~10/30 만 존재)

규칙
  A(현행) 완성 3분봉 종가 net <= 손절선 -> 즉시 청산.
          손절선은 진입 세션 기준 MORNING -1.3% / AFTERNOON -1.2% (config X2LITE / TW).
  B(변형) 진입 후 30분 이내의 '첫 손절 신호'는 경고만 기록하고 1개 3분봉 더 본다.
          · 다음 완성 3분봉 종가도 손절선 이하 -> 그 종가로 청산(CONFIRMED)
          · 손절선 위로 회복 -> 보유 유지(SURVIVE)
          · 대기 중 -2.5% hard stop 터치 -> 즉시 청산
          · 진입 후 30분 경과한 손절은 A 와 동일(무변경)

SURVIVE 이후의 청산은 보수적 대리규칙(shake_tolerance 와 동일 조건):
  net >= -0.2% 회복(CHOP_STOP_RECOVER_PCT) / 3분봉 종가 손절선 재도달 / -2.5% / 20분 경과.
  즉 '회복 후 추가 상승은 0' 으로 본다 -> 규칙 B 에 불리한 방향이다.
  함께 출력하는 B_opt 는 같은 거래를 60분 안의 최고 1분봉 종가에 판 상한값(달성 불가).

주의(stoploss15 §3): 오프라인 3분봉 종가는 재생보다 약 0.05%p 낙관적이다.
  경계(|net - 손절선| < 0.15%p) 거래는 BORDER 로 표시하고 별도 집계한다 — 재생 없이는 확정 못한다.
"""
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
LEDGER = Path(os.environ.get(
    "A_OUT", r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out"))
BAR_DIRS = [ROOT / "data/cache",
            ROOT / "research_20261002_sept_strategy_replay/data",
            ROOT / "research_20261020_h50_hold/data"]
MU_FLAGS = ROOT / "research_20261104_mu_primary/out/mu_flags.csv"
OUT = Path(__file__).resolve().parents[1] / "out"

FEE = 0.000036
STOP_MORNING = -1.3          # config.X2LITE_MORNING_STOP_LOSS
STOP_AFTERNOON = -1.2        # config.AFTERNOON_STOP_LOSS
AFTERNOON_FROM = pd.Timestamp("13:00").time()   # time_window_filter.TW_WINDOW5_START
EARLY_MIN = 30               # 규칙 B 적용 창 (진입 후)
HARD = -2.5                  # 대기 중 hard stop
RECOVER = -0.2               # config.CHOP_STOP_RECOVER_PCT
WAIT_MIN = 20                # SURVIVE 이후 보수적 대리청산 상한
OPT_MIN = 60                 # 상한값 B_opt 탐색 창
BORDER = 0.15                # 경계 판정 폭(%p)
SESSION_END = "15:00"
FROM_DAY = "20260803"
SYM_KIND = {"0193T0": "long", "0197X0": "inverse"}


# ── 공통 ────────────────────────────────────────────────────────────────
def net(px, e):
    return (px / e - 1) * 100 - FEE * 100 * (1 + px / e)


def px_at_net(target, e):
    """net% == target 이 되는 가격 (hard stop 터치 체결가 근사)."""
    return e * (target + 100 + FEE * 100) / (100 - FEE * 100)


def leg_pnl(q, e, x):
    return q * (x - e) - FEE * q * (e + x)


def stop_line(entry_t):
    return STOP_AFTERNOON if entry_t.time() >= AFTERNOON_FROM else STOP_MORNING


def find_bars(day, kind):
    for d in BAR_DIRS:
        f = d / f"replay_{day}_{kind}_1m.csv"
        if f.exists():
            b = pd.read_csv(f, parse_dates=["datetime"])
            b["datetime"] = pd.to_datetime(b["datetime"]).dt.tz_localize(None)
            return b.set_index("datetime").sort_index()
    return None


def bars3(b):
    return b.resample("3min", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


# ── 원장 → 에피소드 ─────────────────────────────────────────────────────
def episodes(js, day):
    eps, cur = [], None
    for o in js["orders"]:
        if o["t"][:10].replace("-", "") != day:
            continue
        if o["side"] == "BUY":
            if cur is not None and cur["left"] > 0:
                cur["buys"].append(o); cur["left"] += o["qty"]; continue
            cur = dict(sym=o["sym"], buys=[o], sells=[], left=o["qty"])
            eps.append(cur)
        elif cur is not None:
            cur["sells"].append(o); cur["left"] -= o["qty"]
    return eps


def stop_legs(js, day):
    """TIME_WINDOW_STOP_LOSS 로 끝난 에피소드의 (진입, 손절 leg, 이전 leg 손익)."""
    out = []
    for ep in episodes(js, day):
        if not ep["sells"]:
            continue
        idx = [i for i, s in enumerate(ep["sells"])
               if any(a.startswith("TIME_WINDOW_STOP_LOSS") for a in (s.get("acts") or []))]
        if not idx:
            continue
        i = idx[-1]
        b = ep["buys"][0]
        e = float(b["px"])
        prior = sum(leg_pnl(s["qty"], e, float(s["px"])) for s in ep["sells"][:i])
        later = ep["sells"][i + 1:]
        s = ep["sells"][i]
        out.append(dict(
            day=day, sym=ep["sym"], entry_t=pd.Timestamp(b["t"]).tz_localize(None),
            entry_px=e, stop_t=pd.Timestamp(s["t"]).tz_localize(None),
            stop_px=float(s["px"]), qty=int(s["qty"]), prior_pnl=prior,
            later_n=len(later),
            later_pnl=sum(leg_pnl(x["qty"], e, float(x["px"])) for x in later)))
    return out


# ── 반사실 진행 ─────────────────────────────────────────────────────────
def hard_fill(e, bar_open):
    """hard stop 체결가 — 봉 안에서 닿았으면 그 레벨, 이미 아래로 열렸으면 시가."""
    return min(px_at_net(HARD, e), bar_open)


def follow(etf, e, t0, stop, *, wait=WAIT_MIN):
    """t0 부터 1분봉을 따라가며 보수적 대리청산. 반환 (가격, 사유)."""
    end = min(t0 + pd.Timedelta(minutes=wait),
              pd.Timestamp(f"{t0.date()} {SESSION_END}"))
    seg = etf[(etf.index >= t0) & (etf.index < end)]
    for ts, r in seg.iterrows():
        if net(float(r.low), e) <= HARD:
            return hard_fill(e, float(r.open)), "HARD"
        c = float(r.close)
        if net(c, e) >= RECOVER:
            return c, "RECOVER"
        tc = ts + pd.Timedelta(minutes=1)
        if tc.minute % 3 == 0 and net(c, e) <= stop:
            return c, "STOP3"
    prev = etf[etf.index < end]
    return (float(prev.iloc[-1].close), "TIMEOUT") if len(prev) else (None, "NODATA")


def variant_b(etf, b3, row, stop, *, early_only=True):
    """규칙 B 의 손절 leg 체결가와 사유."""
    e, t_stop = row["entry_px"], row["stop_t"]
    held = (t_stop - row["entry_t"]).total_seconds() / 60.0
    if early_only and held > EARLY_MIN:
        return row["stop_px"], "LATE=A", None, None

    # 손절 판정 시점에 이미 hard stop 아래면 유예 자체가 불가능 -> A 와 동일.
    if net(row["stop_px"], e) <= HARD:
        return row["stop_px"], "HARD@TRIG=A", None, None

    nxt = b3[b3.index >= t_stop]              # 대기할 다음 3분봉 (index=봉 시작)
    if not len(nxt):
        return row["stop_px"], "NOBAR=A", None, None
    nb = nxt.iloc[0]
    nb_end = nxt.index[0] + pd.Timedelta(minutes=3)
    nb_net = net(float(nb.close), e)

    # 대기 구간(손절시각 ~ 다음 봉 종료) hard stop 터치
    seg = etf[(etf.index >= t_stop) & (etf.index < nb_end)]
    if len(seg) and net(float(seg.low.min()), e) <= HARD:
        hit = seg[seg.low.apply(lambda x: net(float(x), e) <= HARD)].iloc[0]
        return hard_fill(e, float(hit.open)), "HARD", nb_net, nb_end

    if nb_net <= stop:
        return float(nb.close), "CONFIRMED", nb_net, nb_end
    px, why = follow(etf, e, nb_end, stop)
    if px is None:
        return row["stop_px"], "NODATA=A", nb_net, nb_end
    return px, "SURVIVE→" + why, nb_net, nb_end


# ── MU 상태 ─────────────────────────────────────────────────────────────
def mu_lookup():
    if not MU_FLAGS.exists():
        return None
    m = pd.read_csv(MU_FLAGS, encoding="utf-8-sig")
    m["decide_t"] = pd.to_datetime(m.day.astype(str) + " " + m.decide, format="%Y%m%d %H:%M")
    return m


#: 원장 entries 의 dir 은 UP_RED/DOWN_BLUE, mu_flags 의 dir 은 RED/BLUE 다.
_DIR = {"UP_RED": "RED", "DOWN_BLUE": "BLUE"}


def mu_state_for(mu, day, entry_t, direction):
    if mu is None or not len(mu[mu.day == int(day)]):
        return "NO_DATA"            # MU 1분봉은 10/19 부터만 존재
    w = mu[(mu.day == int(day)) & (mu.decide_t <= entry_t)]
    d = _DIR.get(direction or "", direction)
    if d:
        w = w[w.dir == d]
    if not len(w):
        return "UNMATCHED"
    return str(w.sort_values("decide_t").iloc[-1].mu_state)


def entry_dirs(js):
    return {pd.Timestamp(x["t"]).tz_localize(None): x.get("dir") for x in js.get("entries", [])}


# ── main ────────────────────────────────────────────────────────────────
def main():
    days = sorted(f.stem.split("_")[-1] for f in LEDGER.glob("L85_A_2026*.json")
                  if f.stem.split("_")[-1] >= FROM_DAY)
    mu = mu_lookup()
    daily_A, daily_B = {}, {}
    rows, missing = [], []

    for day in days:
        js = json.loads((LEDGER / f"L85_A_{day}.json").read_text(encoding="utf-8"))
        dirs = entry_dirs(js)
        tot = 0.0
        for ep in episodes(js, day):
            if not ep["sells"]:
                continue
            e = float(ep["buys"][0]["px"])
            tot += sum(leg_pnl(s["qty"], e, float(s["px"])) for s in ep["sells"])
        daily_A[day] = tot
        daily_B[day] = tot

        for r in stop_legs(js, day):
            kind = SYM_KIND.get(r["sym"])
            etf = find_bars(day, kind) if kind else None
            A_leg = leg_pnl(r["qty"], r["entry_px"], r["stop_px"])
            if etf is None or not len(etf):
                missing.append((day, r["sym"]))
                continue
            b3 = bars3(etf)
            stop = stop_line(r["entry_t"])

            # 재구성 검증: 손절시각 직전 완성 3분봉
            prev = b3[b3.index + pd.Timedelta(minutes=3) <= r["stop_t"]]
            trig_net = net(float(prev.iloc[-1].close), r["entry_px"]) if len(prev) else None

            px_b, why_b, nb_net, nb_end = variant_b(etf, b3, r, stop)
            B_leg = leg_pnl(r["qty"], r["entry_px"], px_b)
            # 시간제한 없이 '모든 손절에 2봉 확인' 을 적용했다면 (30분 조건의 가치 측정용)
            px_all, why_all, nb_all, _ = variant_b(etf, b3, r, stop, early_only=False)
            Ball_leg = leg_pnl(r["qty"], r["entry_px"], px_all)

            seg = etf[(etf.index > r["stop_t"])
                      & (etf.index <= r["stop_t"] + pd.Timedelta(minutes=OPT_MIN))]
            opt_px = float(seg.close.max()) if len(seg) else r["stop_px"]

            daily_B[day] += (B_leg - A_leg)
            held = (r["stop_t"] - r["entry_t"]).total_seconds() / 60.0
            rows.append(dict(
                day=day, sym="레버" if kind == "long" else "인버스",
                entry=r["entry_t"].strftime("%H:%M:%S"), stop=r["stop_t"].strftime("%H:%M:%S"),
                held=round(held, 1), early=held <= EARLY_MIN, stop_line=stop,
                A=A_leg, A_net=round(net(r["stop_px"], r["entry_px"]), 2),
                trig_net=None if trig_net is None else round(trig_net, 2),
                nb_net=None if nb_net is None else round(nb_net, 2),
                recovered=None if nb_net is None else bool(nb_net > stop),
                border=None if nb_net is None else bool(abs(nb_net - stop) < BORDER),
                B=B_leg, B_why=why_b, dB=B_leg - A_leg,
                dBall=Ball_leg - A_leg, Ball_why=why_all,
                nb_all=None if nb_all is None else round(nb_all, 2),
                B_opt=leg_pnl(r["qty"], r["entry_px"], opt_px) - A_leg,
                mu=mu_state_for(mu, day, r["entry_t"], dirs.get(r["entry_t"]))))

    d = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    d.to_csv(OUT / "cf_58d.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 260)
    print(f"운영구성(L85_A) {len(days)}일 {days[0]}~{days[-1]} — TIME_WINDOW_STOP_LOSS {len(d)}건"
          f"{'  (1분봉 없음 %d건 제외)' % len(missing) if missing else ''}\n")
    print(d[["day", "entry", "stop", "sym", "held", "stop_line", "A", "A_net", "trig_net",
             "nb_net", "recovered", "border", "B", "B_why", "dB", "B_opt", "mu"]]
          .to_string(index=False, float_format=lambda x: f"{x:,.0f}" if abs(x) > 100 else f"{x:.2f}"))

    print("\n[재구성 검증] 손절 트리거 봉 종가가 손절선 이하로 재현되는가")
    ok = d[d.trig_net.notna() & (d.trig_net <= d.stop_line)]
    print(f"  재현 {len(ok)}/{d.trig_net.notna().sum()}건"
          f"   재현 실패(모델이 낙관): {sorted(set(d[d.trig_net.notna() & (d.trig_net > d.stop_line)].day))}")

    def agg(sub, label):
        if not len(sub):
            print(f"  {label:34s} 0건")
            return
        dd = sub.dB
        top = sorted(dd, reverse=True)
        print(f"  {label:34s} {len(sub):2d}건  Δ합 {dd.sum():+10,.0f}  "
              f"개선 {int((dd > 0).sum())} / 악화 {int((dd < 0).sum())} / 동일 {int((dd == 0).sum())}  "
              f"최악Δ {dd.min():+9,.0f}  상위1제외 {dd.sum() - sum(top[:1]):+10,.0f}  "
              f"상위2제외 {dd.sum() - sum(top[:2]):+10,.0f}  상한Δ합 {sub.B_opt.sum():+10,.0f}")

    print("\n[부분집합별 효과]  Δ = B − A (손절 leg 기준)")
    agg(d, "전체 손절")
    agg(d[d.early], f"진입 후 {EARLY_MIN}분 이내 (규칙 B 대상)")
    agg(d[~d.early], f"{EARLY_MIN}분 초과 (B 는 A 와 동일)")
    agg(d[d.early & (d.mu == "CONFIRM")], "  └ 그 중 MU CONFIRM")
    agg(d[d.early & d.mu.isin(["CONFIRM", "STALL"])], "  └ 그 중 MU non-DIVERGE")
    agg(d[d.early & (d.mu == "DIVERGE")], "  └ 그 중 MU DIVERGE(=BAD)")
    agg(d[d.early & (d.mu.isin(["NO_DATA", "UNMATCHED"]))], "  └ 그 중 MU 판정 불가")
    dd = d.dBall
    print(f"  {'[참고] 30분 조건 없이 전 손절 적용':32s} {len(d):2d}건  Δ합 {dd.sum():+10,.0f}  "
          f"개선 {int((dd > 0).sum())} / 악화 {int((dd < 0).sum())} / 동일 {int((dd == 0).sum())}")

    e = d[d.early]
    if len(e) and e.nb_net.notna().any():
        rec = e[e.recovered.notna()]
        print(f"\n[첫 손절 후 다음 3분봉 회복률] {int(rec.recovered.sum())}/{len(rec)}건"
              f" = {100 * rec.recovered.mean():.0f}%   경계(±{BORDER}%p) {int(rec.border.sum())}건")
        for st in ["CONFIRM", "STALL", "DIVERGE", "NO_DATA", "-"]:
            s = rec[rec.mu == st]
            if len(s):
                print(f"    MU {st:9s} {int(s.recovered.sum())}/{len(s)}건  Δ합 {s.dB.sum():+10,.0f}")

    def mdd(dmap):
        cum = peak = m = 0.0
        for k in days:
            cum += dmap[k]; peak = max(peak, cum); m = max(m, peak - cum)
        return m

    tA, tB = sum(daily_A.values()), sum(daily_B.values())
    wA = min(daily_A.values()); wB = min(daily_B.values())
    print(f"\n[전체 성과] A -> B")
    print(f"  총손익  {tA:+,.0f} -> {tB:+,.0f}   Δ {tB - tA:+,.0f}")
    print(f"  MDD     {mdd(daily_A):,.0f} -> {mdd(daily_B):,.0f}   Δ {mdd(daily_B) - mdd(daily_A):+,.0f}")
    print(f"  최악일  {wA:+,.0f} -> {wB:+,.0f}")
    print(f"  손실일수 {sum(1 for v in daily_A.values() if v < 0)} -> "
          f"{sum(1 for v in daily_B.values() if v < 0)}")
    if len(d):
        print(f"  최악 손절거래  A {d.A.min():+,.0f} -> B {d.B.min():+,.0f}")
    if missing:
        print(f"\n[1분봉 없음] {missing}")


if __name__ == "__main__":
    main()
