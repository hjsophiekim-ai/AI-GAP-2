"""9월 MACD2 청산 구조 비교: R0(현행 P3) / X1 SIMPLE EXIT / X2 SIMPLE EXIT + DELAYED REVERSE. READ-ONLY.

R0 : production worker.run_once 재생(P3) 결과 그대로 (wk/out/wk_<D>.json).
X1/X2: 같은 재생의 신호원장(T+3 확인행)을 진입·반대플래그 후보로 쓰고, 보유 이후만 아래 규칙으로 다시 계산.
  * 진입 후보 = 재생에서 EXECUTED 된 확인행 (production 진입게이트 통과가 확인된 것만).
    [민감도 SLOT] 하루 3회 한도로만 막혔던 확인행(TW2_3SLOT_REJECT_DAILY_SLOT_CAP)도 진입 후보로 포함.
  * 반대 플래그 = T+3 확인을 통과한 반대방향 확인행 (진입게이트 거절이어도 포함 -- production 의
    '반대 확인 플래그 + 진입거절 = 청산만' 경로와 같은 의미). NOT_CONFIRMED / GAP_NOT_EXPANDING 은 제외.
  * 반대 플래그 시 production H50 함수(small_whipsaw_hold.evaluate_hold) 로 HOLD 판정.
    HOLD 면 완성 3분봉마다 evaluate_release (추세붕괴 2봉 / 60분) -> 해제 시 청산.
  * 그 외 청산은 15:00 강제청산뿐 (B3 ±1%, 20분, Q2, Y3, N1 래더, 손절 없음).
  * X1: 청산 후 반대 ETF 즉시 매수 없음. 다음 신규 진입 후보에서만 진입.
  * X2: 반대 플래그가 '진입 후보'(EXECUTED) 였으면, 완성 3분봉 1개 뒤 MACD hist 부호가 여전히
    그 방향일 때만 진입 (슬롯 남아 있을 때). 아니면 flat.
  * 하루 3회 진입 한도 유지. 체결 = 재생 FakeBroker 와 같은 규칙(그 tick 에 보이는 마지막 완성 1분봉 종가).
  * 수량 = 같은 신호의 재생 수량, 재생에 없던 진입은 1,000만원/가격. 수수료 왕복 0.03%.
"""
import glob, json, os, sys
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
SLOTSENS = "--slot" in sys.argv
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
from app.trading.macd2 import config, small_whipsaw_hold as H50, signal_engine as se  # noqa: E402
from app.trading.macd2.market_data import filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
K = config.KST
FEE = 0.03
LONG, INV = config.LONG_SYMBOL, config.INVERSE_SYMBOL
NOT_T3 = {"REJECT_NOT_CONFIRMED", "REJECT_MACD_GAP_NOT_EXPANDING"}
DAYS = sorted(os.path.basename(p)[3:11] for p in glob.glob(S + "/wk/out/wk_2026*.json")
              if len(os.path.basename(p)) == len("wk_20260903.json"))
DAYS = [d for d in DAYS if d.startswith("202609")]


def rd(d, tag):
    x = pd.read_csv(f"{S}/wk/data/replay_{d}_{tag}_1m.csv")
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19]).dt.tz_localize(K)
    return x.sort_values("datetime").reset_index(drop=True)


def prev_days(d):
    allf = sorted({os.path.basename(p)[7:15] for p in glob.glob(S + "/wk/data/replay_*_hynix_1m.csv")})
    i = allf.index(d)
    return allf[max(0, i - 2):i]


def bars_at(hy, now):
    df = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
    df, _ = se.exclude_preopen_padding_1m(df)
    b = se.resample_completed_3m(df, now)
    b, _ = filter_complete_3m_bars(b, df)
    return b.reset_index(drop=True)


def px(e, t):
    s = e[e["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)]
    return float(s["close"].iloc[-1]) if len(s) else None


def load_r0(d):
    o = json.load(open(f"{S}/wk/out/wk_{d}.json", encoding="utf-8"))
    rows = []
    for s in o["sig"]:
        sid = s["signal_id"] or ""
        if ":TW2_3SLOT_CONFIRM" not in sid:
            continue
        rows.append(dict(t=pd.Timestamp(s["detected_at"]).tz_convert(K) if pd.Timestamp(s["detected_at"]).tzinfo else pd.Timestamp(s["detected_at"]).tz_localize(K),
                         f=1 if "UP_RED" in sid else -1, sid=sid.split(":")[0], res=s["order_result"], br=s["block_reason"] or ""))
    rows.sort(key=lambda r: r["t"])
    qty = {}
    for r in o["orders"]:
        if r["side"] == "BUY":
            qty[pd.Timestamp(r["t"]).strftime("%H:%M")] = int(r["qty"])
    return o, rows, qty


def r0_trades(o):
    out, cur = [], None
    for r in o["orders"]:
        if r["side"] == "BUY":
            cur = dict(t=r["t"][11:16], sym=r["sym"], q=r["qty"], px=r["px"], sold=0, pr=0.0, xt=None)
        elif cur:
            cur["sold"] += r["qty"]; cur["pr"] += r["qty"] * r["px"]; cur["xt"] = r["t"][11:16]
        if cur and cur["sold"] >= cur["q"]:
            out.append(dict(entry=cur["t"], dir="UP" if cur["sym"] == LONG else "DN", px_in=cur["px"],
                            exit=cur["xt"], px_out=cur["pr"] / cur["q"], q=cur["q"],
                            net=(cur["pr"] / cur["q"] / cur["px"] - 1) * 100 - FEE,
                            krw=cur["pr"] - cur["q"] * cur["px"] - (cur["pr"] + cur["q"] * cur["px"]) * FEE / 200))
            cur = None
    return out


def sim_day(d, mode):
    o, rows, qty = load_r0(d)
    hy = pd.concat([rd(x, "hynix") for x in prev_days(d) + [d]]).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    etf = {1: rd(d, "long"), -1: rd(d, "inverse")}
    day0 = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00", tz=K)
    force = day0 + pd.Timedelta(hours=15)
    cand = [r for r in rows if r["res"] == "EXECUTED" or (SLOTSENS and r["br"] == "TW2_3SLOT_REJECT_DAILY_SLOT_CAP")]
    opp_rows = [r for r in rows if r["br"] not in NOT_T3 and r["res"] != "TIME_WINDOW_WHIPSAW_HOLD"]
    trades, pos, used = [], None, 0
    pending = None   # X2 지연 진입 {f, at, sid}
    events = sorted([(r["t"], "flag", r) for r in opp_rows] + [(r["t"], "cand", r) for r in cand], key=lambda e: (e[0], e[1] == "cand"))
    # 완성 3분봉 경계 tick (H50 해제 / X2 지연 판정용)
    ticks = pd.date_range(day0 + pd.Timedelta(hours=9, minutes=3), force, freq="3min")
    timeline = sorted([(t, "tick", None) for t in ticks] + events, key=lambda e: (e[0], {"tick": 0, "flag": 1, "cand": 2}[e[1]]))

    def close(t, why):
        nonlocal pos
        p = px(etf[pos["f"]], t)
        net = (p / pos["px_in"] - 1) * 100 - FEE
        krw = pos["q"] * (p - pos["px_in"]) - pos["q"] * (p + pos["px_in"]) * FEE / 200
        pos.update(exit=t, px_out=p, why=why, net=net, krw=krw)
        trades.append(pos); pos = None

    def enter(t, f, sid, how):
        nonlocal pos, used
        p = px(etf[f], t)
        if p is None:
            return
        q = qty.get(pd.Timestamp(t).strftime("%H:%M")) or int(10_000_000 // p)
        pos = dict(day=d, entry=t, f=f, px_in=p, q=q, sid=sid, how=how, opp_at=None, h50=None, h50_rel=None)
        used += 1

    done_sid = set()
    for t, kind, r in timeline:
        if t > force:
            break
        if kind == "tick":
            if pos is not None and pos.get("h50_start") is not None:
                b = bars_at(hy, t)
                rel = H50.evaluate_release(b, Direction.UP_RED if pos["f"] == 1 else Direction.DOWN_BLUE,
                                           pos["h50_start"].to_pydatetime(), t.to_pydatetime(), pos.get("h50_cnt", 0))
                pos["h50_cnt"] = rel.trend_break_count
                if rel.should_release:
                    pos["h50_rel"] = rel.reason
                    close(t, "H50_RELEASE:" + rel.reason)
            if pending is not None and t >= pending["at"]:
                b = bars_at(hy, t)
                s = se.calculate_macd(b)
                ok = s is not None and np.sign(float(s.current_diff)) == pending["f"]
                if ok and pos is None and used < 3:
                    enter(t, pending["f"], pending["sid"], "X2_DELAYED")
                else:
                    trades_note.append((d, pending["sid"], "X2 지연진입 취소" + ("" if ok else "(1봉 뒤 방향 소멸)")))
                pending = None
            continue
        if kind == "flag" and pos is not None and r["f"] != pos["f"] and pos.get("h50_start") is None:
            pos["opp_at"] = t
            b = bars_at(hy, t)
            hd = H50.evaluate_hold(b, Direction.UP_RED if pos["f"] == 1 else Direction.DOWN_BLUE, t.to_pydatetime())
            if hd.should_hold:
                pos["h50"] = "HOLD"; pos["h50_start"] = t; pos["h50_cnt"] = 0
                continue
            pos["h50"] = "NO(" + hd.reason + ")"
            close(t, "OPPOSITE_T3")
            if mode == "X2" and r["res"] == "EXECUTED" and used < 3:
                pending = dict(f=r["f"], at=t + pd.Timedelta(minutes=3), sid=r["sid"])
            continue
        if kind == "cand" and pos is None and used < 3 and r["sid"] not in done_sid and pending is None:
            enter(t, r["f"], r["sid"], "NEW")
            done_sid.add(r["sid"])
    if pos is not None:
        close(force, "FORCED_15:00")
    return trades


def stats(daily_krw, trades):
    v = np.array(daily_krw, dtype=float) / 10_000_000 * 100
    eq = np.cumprod(1 + v / 100); dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
    k = np.array([t["krw"] for t in trades]) if trades else np.array([0.0])
    pf = k[k > 0].sum() / max(-k[k < 0].sum(), 1)
    return dict(krw=sum(daily_krw), comp=(eq[-1] - 1) * 100, pf=pf, mdd=dd,
                win=(np.array([t["net"] for t in trades]) > 0).mean() * 100 if trades else 0,
                lossd=int((v < 0).sum()), d2=int((v >= 2).sum()), n=len(trades))


trades_note = []
res = {"R0": {}, "X1": {}, "X2": {}}
for d in DAYS:
    o, _, _ = load_r0(d)
    res["R0"][d] = r0_trades(o)
    res["X1"][d] = sim_day(d, "X1")
    res["X2"][d] = sim_day(d, "X2")
print(f"9월 대상일 {len(DAYS)}일: {' '.join(d[4:] for d in DAYS)}" + ("   [민감도: 슬롯한도 행도 진입후보]" if SLOTSENS else ""))
print("\n| 후보 | 총손익(원) | 복리% | PF | MDD% | 승률% | 손실일 | +2%이상일 | 거래수 |")
print("|---|---|---|---|---|---|---|---|---|")
for k in ("R0", "X1", "X2"):
    tr = [t for d in DAYS for t in res[k][d]]
    st = stats([sum(t["krw"] for t in res[k][d]) for d in DAYS], tr)
    print(f"| {k} | {st['krw']:+,.0f} | {st['comp']:+.2f} | {st['pf']:.2f} | {st['mdd']:.2f} | {st['win']:.1f} | {st['lossd']} | {st['d2']} | {st['n']} |")
print("\n일별 손익(원)")
for d in DAYS:
    print(f"  {d}  R0 {sum(t['krw'] for t in res['R0'][d]):+10,.0f}   X1 {sum(t['krw'] for t in res['X1'][d]):+10,.0f}   X2 {sum(t['krw'] for t in res['X2'][d]):+10,.0f}")
for k in ("X1", "X2"):
    print(f"\n### {k} 거래별")
    for d in DAYS:
        for t in res[k][d]:
            print(f"  {d} {t['entry']:%H:%M} {'UP 레버' if t['f'] == 1 else 'DN 인버'} {t['q']}@{t['px_in']:.0f} [{t['how']}] 반대플래그 {t['opp_at'].strftime('%H:%M') if t['opp_at'] is not None else '-'} H50 {t['h50'] or '-'}"
                  f" -> {t['exit']:%H:%M}@{t['px_out']:.0f} {t['why']}  net {t['net']:+.2f}% ({t['krw']:+,.0f}원)")
# Q2: H50 이 살린 whipsaw (X1): HOLD 후 청산가가 반대플래그 시점 가격보다 보유방향으로 유리
saved, hurt = [], []
rec = []
for d in DAYS:
    etf = {1: rd(d, "long"), -1: rd(d, "inverse")}
    for t in res["X1"][d]:
        if t["h50"] == "HOLD":
            p_opp = px(etf[t["f"]], t["opp_at"])
            (saved if t["px_out"] > p_opp else hurt).append((d, t["entry"].strftime("%H:%M"), p_opp, t["px_out"]))
        if t["why"] == "OPPOSITE_T3":
            e = etf[t["f"]]
            after = e[(e["datetime"] >= t["exit"]) & (e["datetime"] <= min(t["exit"] + pd.Timedelta(minutes=60), pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00", tz=K)))]
            if len(after) and after["high"].max() >= t["px_out"] * 1.01:
                rec.append((d, t["entry"].strftime("%H:%M"), t["exit"].strftime("%H:%M"), t["px_out"], float(after["high"].max())))
print(f"\nQ2 H50 HOLD {len(saved) + len(hurt)}건: 살림 {len(saved)} / 손해 {len(hurt)}  살림={saved}  손해={hurt}")
print(f"Q3 X1 반대플래그 즉시청산 후 60분 안에 원래방향 +1% 이상 회복: {len(rec)}건  {rec}")
print("X2 메모:", trades_note)
