"""최근 거래 손실 원인 분해 (2026-10-17). READ-ONLY, production 무수정.

질문: 최근 5~10일 손실 거래는 (a) 진입 자체가 틀린 것인가 (b) 수익 반납인가
      (c) execution(체결) 지연 때문인가.

원장: research_20261016_pmblue_whip 에서 만든 BASE 재생 원장 89일
      (= production 현행 E + SLOW-TP2, WK_FILL=REAL).
경로: ETF 1분봉 high/low 로 MAE/MFE 를 만든다 (bb/tw1.py 와 같은 계산).

사전 고정 분류 (임계값은 production 상수에서 가져온다 — 탐색 없음):
  mfe_b = 최저점(MAE) 찍기 전까지의 최대 평가수익 % (수수료 반영)
  · A 즉시역행 : mfe_b < 0.3            — 한 번도 의미있게 유리해진 적 없음
  · B 목표미달 : 0.3 <= mfe_b < 1.0     — 시스템 최소 익절선(P3_B3_TP_PCT=1.0) 미달
  · C 수익반납 : mfe_b >= 1.0           — 최소 익절선을 넘겼는데 손실 마감
execution 지표:
  slip  = '신호 확정시각 1분봉 종가' 대비 체결가 유불리 %(음수 = 비싸게 샀다)
  delay = 확정(detected_at) -> 실제 체결 주문시각 분
  is_brk/brk_wait = E 돌파대기(:BRK)로 의도적으로 기다린 건인지와 그 대기분
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
LED = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk/out"
BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
SYM2TAG = {"0193T0": "long", "0197X0": "inverse"}
B3_TP = 1.0
IMMEDIATE = 0.3


def bars(day, sym):
    p = f"{BARS}/replay_{day}_{SYM2TAG[sym]}_1m.csv"
    if not os.path.exists(p):
        return None
    x = pd.read_csv(p)
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    return x.drop_duplicates("datetime", keep="last").sort_values("datetime")


def trades(o):
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            if cur is not None and cur["sold"] < cur["q"]:
                tot = cur["q"] + x["qty"]
                cur["px_in"] = (cur["px_in"] * cur["q"] + x["px"] * x["qty"]) / tot
                cur["q"] = tot
                continue
            cur = dict(day=o["D"], entry=t, px_in=float(x["px"]), q=int(x["qty"]),
                       sold=0, krw=0.0, reasons=[], exit=None, sym=x["sym"], legs=[])
            out.append(cur)
            continue
        e = ex_s[si]
        si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]
        cur["legs"].append((t, int(x["qty"]), float(x["px"])))
        cur["krw"] += float(e["net_pnl"] or 0)
        cur["reasons"].append(e["exit_reason"] or "?")
        cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur = None
    return [t for t in out if "net" in t]


def build(days):
    rows = []
    for d in days:
        p = f"{LED}/BASE_{d}.json"
        if not os.path.exists(p):
            continue
        o = json.load(open(p, encoding="utf-8"))
        sig = {pd.Timestamp(s["detected_at"]).tz_localize(None): s
               for s in o["sig"] if s["order_result"] == "EXECUTED"}
        fired = {e["sid"]: e for e in o.get("brk", []) if e.get("ev") == "FIRED"}
        for t in trades(o):
            e0 = pd.Timestamp(t["entry"]).tz_localize(None)
            e1 = pd.Timestamp(t["exit"]).tz_localize(None)
            cost = t["net"] - (float(t["legs"][-1][2]) / t["px_in"] - 1) * 100
            cand = [k for k in sig if abs((k - e0).total_seconds()) <= 600]
            s = sig[min(cand, key=lambda k: abs((k - e0).total_seconds()))] if cand else None
            sid = str(s["signal_id"]) if s else ""
            dec = pd.Timestamp(s["detected_at"]).tz_localize(None) if s else pd.NaT
            b = bars(d, t["sym"])
            px_sig = np.nan
            if b is not None and pd.notna(dec):
                w = b[b.datetime <= dec]
                if len(w):
                    px_sig = float(w.close.iloc[-1])
            r = dict(day=d, sym=t["sym"], dir=("UP" if t["sym"] == "0193T0" else "DN"),
                     entry=e0.strftime("%H:%M"), exit=e1.strftime("%H:%M"),
                     krw=t["krw"], net=t["net"], hold=t["hold"],
                     reason="+".join(dict.fromkeys(t["reasons"])),
                     sid=sid.replace(":TW2_3SLOT_CONFIRM", "").replace(":BRK", ""),
                     is_brk=int(sid.endswith(":BRK")),
                     dec=(dec.strftime("%H:%M") if pd.notna(dec) else ""),
                     delay=((e0 - dec).total_seconds() / 60 if pd.notna(dec) else np.nan),
                     px_in=t["px_in"], px_sig=px_sig,
                     slip=((px_sig / t["px_in"] - 1) * 100 if px_sig == px_sig else np.nan),
                     brk_wait=(fired[sid].get("delay_min") if sid in fired else np.nan))
            if b is None:
                r.update(mae=np.nan, mfe=np.nan, mfe_b=np.nan, t_mae=np.nan, t_mfe=np.nan)
            else:
                w = b[(b.datetime >= e0) & (b.datetime <= e1)]
                if len(w):
                    lo = (w.low.to_numpy(float) / t["px_in"] - 1) * 100 + cost
                    hi = (w.high.to_numpy(float) / t["px_in"] - 1) * 100 + cost
                    i_lo, i_hi = int(np.argmin(lo)), int(np.argmax(hi))
                    r.update(mae=lo.min(), mfe=hi.max(), mfe_b=hi[:i_lo + 1].max(),
                             t_mae=(w.datetime.iloc[i_lo] - e0).total_seconds() / 60,
                             t_mfe=(w.datetime.iloc[i_hi] - e0).total_seconds() / 60)
                else:
                    r.update(mae=np.nan, mfe=np.nan, mfe_b=np.nan, t_mae=np.nan, t_mfe=np.nan)
            rows.append(r)
    df = pd.DataFrame(rows)
    df["cls"] = np.where(df.mfe_b < IMMEDIATE, "A 즉시역행",
                         np.where(df.mfe_b < B3_TP, "B 목표미달", "C 수익반납"))
    # 체결불리가 그 거래 손익에 끼친 금액 (체결가 대신 신호가에 샀다면의 차액)
    df["slip_krw"] = -df.slip / 100 * df.px_in * df.krw / df.krw
    df["slip_krw"] = np.where(df.px_sig == df.px_sig,
                              (df.px_sig - df.px_in) / df.px_in * (df.krw / (df.net / 100).replace(0, np.nan)),
                              np.nan)
    return df


def report(df, title):
    L = df[df.krw < 0]
    print(f"\n{'=' * 90}\n{title} — 거래 {len(df)}건 / 손실 {len(L)}건 "
          f"({len(L) / max(len(df), 1) * 100:.0f}%) / 합 {df.krw.sum():+,.0f} "
          f"(손실분 {L.krw.sum():+,.0f})\n{'=' * 90}")
    if not len(L):
        return
    print("\n[손실 원인 분해]  mfe_b = 최저점 찍기 전까지의 최대 평가수익 %")
    g = L.groupby("cls").agg(건수=("krw", "size"), 손실합=("krw", "sum"),
                             평균net=("net", "mean"), 평균mfe_b=("mfe_b", "mean"),
                             평균보유=("hold", "mean"))
    g["손실비중%"] = (g.손실합 / L.krw.sum() * 100).round(0)
    print(g.to_string(float_format="{:,.2f}".format))
    print("\n[청산사유별 손실]")
    print(L.groupby("reason").agg(건수=("krw", "size"), 손실합=("krw", "sum"),
                                  평균mfe_b=("mfe_b", "mean")).sort_values("손실합").to_string(float_format="{:,.2f}".format))
    print("\n[execution]  slip = 신호시각 종가 대비 체결가 유불리 %(음수 = 비싸게 삼)")
    for lab, g2 in (("손실거래", L), ("이익거래", df[df.krw > 0]), ("전체", df)):
        if not len(g2):
            continue
        print(f"  {lab:8s} n {len(g2):3d}  slip 평균 {g2.slip.mean():+.3f}% 중앙 {g2.slip.median():+.3f}% "
              f"최악 {g2.slip.min():+.3f}%  |  신호→체결 지연 평균 {g2.delay.mean():.1f}분 최대 {g2.delay.max():.1f}분"
              f"  |  돌파대기 {int(g2.is_brk.sum())}건")
    ls = L.dropna(subset=["slip_krw"])
    print(f"  손실거래의 체결불리 금액 합 {ls.slip_krw.sum():+,.0f}원 "
          f"(= 신호시각 종가에 샀다면 달라졌을 금액, 손실합 {L.krw.sum():+,.0f} 대비 "
          f"{ls.slip_krw.sum() / abs(L.krw.sum()) * 100:.1f}%)")
    print("\n[개별 손실거래]")
    cols = ["day", "dec", "entry", "exit", "dir", "krw", "net", "mfe_b", "mfe", "mae",
            "t_mfe", "hold", "delay", "slip", "slip_krw", "is_brk", "brk_wait", "cls", "reason"]
    print(L.sort_values("krw")[cols].round(2).to_string(index=False))


def main():
    days = sorted(os.path.basename(p)[5:-5] for p in glob.glob(LED + "/BASE_*.json"))
    df = build(days)
    df.to_csv(OUT + "/d1_trades_89d.csv", index=False, encoding="utf-8-sig")
    print(f"BASE 재생 89일 거래 {len(df)}건 (05-27~10-08)")
    for n in (5, 10):
        report(df[df.day.isin(days[-n:])], f"최근 {n}영업일 ({days[-n]}~{days[-1]})")
    report(df[df.day >= "20260801"], "8~10월 전체 (43일, 비교용)")
    report(df, "89일 전체 (비교용)")


if __name__ == "__main__":
    main()
