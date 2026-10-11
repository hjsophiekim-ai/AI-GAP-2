"""b3run — 변형 실행기.  python b3run.py BASE | <variant ...>

변형 정의 (공통: SL 1.0 / max-hold 20분 / SHADOW-BASE SLOW detector):
  T10/T11/T12       : B3 단독 (P3/Y3 OFF), TP 1.0/1.1/1.2
  P3_T10            : 현재 P3 stack (B3 TP1.0 + P3 trig1.0/6분 + Y3)
  P3A_T11/P3A_T12   : B3 TP만 변경, P3 trig 1.0 유지  (판정용)
  P3B_T11/P3B_T12   : P3 trig 도 TP 와 같이 이동      (참고용)
"""
import pickle
import sys
import time

import b3lib as L
from common import summarize

_HVON = pickle.load(open(L.HERE / "hv_on.pkl", "rb")) if (L.HERE / "hv_on.pkl").exists() else {}
_HV3A = pickle.load(open(L.HERE / "hv_on3a.pkl", "rb")) if (L.HERE / "hv_on3a.pkl").exists() else {}
_HV3B = pickle.load(open(L.HERE / "hv_on3b.pkl", "rb")) if (L.HERE / "hv_on3b.pkl").exists() else {}
VAR = {
    "T10": dict(tp=1.0, p3=False, y3=False),
    "T11": dict(tp=1.1, p3=False, y3=False),
    "T12": dict(tp=1.2, p3=False, y3=False),
    "P3_T10": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "P3A_T11": dict(tp=1.1, p3=True, p3_trig=1.0, y3=True),
    "P3A_T12": dict(tp=1.2, p3=True, p3_trig=1.0, y3=True),
    "P3B_T11": dict(tp=1.1, p3=True, p3_trig=1.1, y3=True),
    "P3B_T12": dict(tp=1.2, p3=True, p3_trig=1.2, y3=True),
    "R0": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "R1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R1"),
    "R2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R2"),
    "R3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R3"),
    "M1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, y3mode="PARTIAL"),
    "M2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, y3mode="OR"),
    "ALL_P3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, all=True),
    "ALL_B3": dict(tp=1.0, p3=False, y3=False, all=True),
    "S0": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "Q2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2),
    "Q3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, y3mode="OR"),
    "Q4": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, h50prom=True),
    "Q5": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50prom=True),
    "H30": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0),
    "SL08": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, sl=0.8),
    "SL12": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, sl=1.2),
    "BE05": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, be_trig=0.5, be_floor=0.0),
    "G10": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, grace="G10"),
    "G10S": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, grace="STRUCT"),
    "T12": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, tp_exit=1.2),
    "Y15F": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                 y3tp={"mode": "FULL", "pct": 1.5}),
    "Y15H": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                 y3tp={"mode": "HALF", "pct": 1.5}),
    "Y15X": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                 y3tp={"mode": "H50EXC", "pct": 1.5}),
    "H40": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=40.0),
    "H60": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=60.0),
    # 2026-09-30: 사용자 R1/R2/R3 (기존 R1~R3 늦은승격 키와 충돌해 RL1/RG2/RC3 로 명명)
    "RL1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                r1={"arm": 2.0, "floor": 1.0}),
    "RG2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, r2=True),
    "RC3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                r1={"arm": 2.0, "floor": 1.0}, r2=True),
    # 2026-09-30 (2차) 고변동 연구: A=RG2(R2), B=EXO(EXIT-ONLY), C30/C50=부분 profit lock
    "EXO": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True),
    "C30": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, plock={"arm": 2.0, "floor": 1.0, "frac": 0.3}),
    "C50": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, plock={"arm": 2.0, "floor": 1.0, "frac": 0.5}),
    "A_C30": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, r2=True, plock={"arm": 2.0, "floor": 1.0, "frac": 0.3}),
    "A_C50": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, r2=True, plock={"arm": 2.0, "floor": 1.0, "frac": 0.5}),
    "B_C30": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True, plock={"arm": 2.0, "floor": 1.0, "frac": 0.3}),
    "B_C50": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True, plock={"arm": 2.0, "floor": 1.0, "frac": 0.5}),
    # 2026-09-30 (3차) HIGH-VOL + EXIT-ONLY: 모든 보유 포지션, 60분 range > 2.35% 일 때만
    "HVB": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0,
                exo=True, exo_all=True, exo_hv=2.35),
    # 2026-09-30 (5차) 손절 후 동일방향 1회 재진입
    "RE50": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, reent=0.5),
    "RE70": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, reent=0.7),
    "RE100": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, reent=1.0),
    # 2026-09-30 (6차) B(EXIT-ONLY) + B진입 손절 후 동일방향 1회 재진입 50%
    "P3BR": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True, exo_scope=["P3R"], reent=0.5, reent_exo_only=True),
    "N1BR": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True, exo_scope=["BASE"], reent=0.5, reent_exo_only=True),
    "BOTHBR": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, exo=True, exo_scope=["P3R", "BASE"], reent=0.5, reent_exo_only=True),
    # 2026-09-30 (7차) B3 SOFT STOP: -1.0 soft / -1.3 hard / 유예 = 도달 봉 완성까지 (기존 S1 키와 충돌 회피로 SOFT1)
    "SOFT1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, soft={"hard": 1.3}),
    # 2026-09-30 (8차) HIGH-VOL DAY MODE (D1 탐지기)
    "HVP": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HVON, hvp=True),
    "HVD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HVON, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"),
    "HVPD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HVON, hvp=True, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"),
    # D3a (ER<0.3) / D3b (ER<=20일 중앙값) 출렁임 탐지기
    "AP": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3A, hvp=True), "AD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3A, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"), "APD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3A, hvp=True, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"),
    "BP": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3B, hvp=True), "BD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3B, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"), "BPD": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0, hv_on=_HV3B, hvp=True, exo=True, exo_all=True, exo_hvday=True, exo_confirm="GAP"),
    "S1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, fs_stop=1.0),
    "S2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, fs_stop=1.1),
}
strict = "--le" not in sys.argv
SFX = "" if L.NDAYS == 80 else f"_d{L.NDAYS}"
names = [a for a in sys.argv[1:] if not a.startswith("--")]

for name in names:
    t0 = time.time()
    if name in ("BASE", "BASE2"):
        ts = L.run(None)
    else:
        base = pickle.load(open(L.HERE / f"out_BASE{SFX}.pkl", "rb"))["trades"]
        cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0)
        cfg.update(VAR[name])
        cfg["regime"] = L.make_regime(base, strict=strict)
        if cfg.pop("all", False):
            cfg["regime"] = lambda _ts: "CHOP"
        ts = L.run(cfg)
    m = summarize(ts, L.DATES)
    tag = name + ("" if strict else "_le") + SFX
    print("%-8s 거래 %3d  복리 %9.4f  PF %.4f  MDD %7.3f  승률 %.2f%%  (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["win_rate_pct"],
             time.time() - t0), flush=True)
    pickle.dump({"trades": ts, "m": m}, open(L.HERE / f"out_{tag}.pkl", "wb"))
