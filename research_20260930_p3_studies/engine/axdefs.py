"""AX 규칙 정의 — 숫자는 전부 저장소에 이미 있는 상수다(새 임계값 없음).

  3.0 config.MORNING_TP1*100 / X2-lite tp1        3.5 config.MORNING_TRAILING_TRIGGER*100 (=N1 tp1)
  4.0 N1 off_tp2 (=N1 오후TP)                     5.0 config.MORNING_TP2*100 (=X2-lite tp2)
  8.0 N1 tp2                                       2.0 X2-lite after_tp1_stop (=config.MORNING_TRAILING_STOP)
  2.8 X2-lite trailing_stop                        1.5 N1 trailing_stop (=config.EARLY_TP_TRIGGER_PCT)

판정식은 tregime.snapshot(...).ok — H50 이 쓰는 EMA20/EMA50/기울기 구조 그대로이며
N1 자신이 이미 tp2 8↔4 전환에 쓰고 있는 바로 그 판정이다. 새 지표식 없음.
"""

AX = {
  # ── A: 진입 후 +3% 도달 완성봉에서 구조를 1회 보고 고정 ────────────────
  "A1_weak_tp2":      {"decide": 3.0, "strong": {}, "weak": {"tp2": 4.0}},
  "A2_weak_floor20":  {"decide": 3.0, "strong": {}, "weak": {"floor": 2.0}},
  "A3_weak_both20":   {"decide": 3.0, "strong": {}, "weak": {"tp2": 4.0, "floor": 2.0}},
  "A4_weak_both28":   {"decide": 3.0, "strong": {}, "weak": {"tp2": 4.0, "floor": 2.8}},
  "A5_strong_stick8": {"decide": 3.0, "strong": {"tp2": 8.0}, "weak": {}},
  "A6_both":          {"decide": 3.0, "strong": {"tp2": 8.0}, "weak": {"tp2": 4.0, "floor": 2.0}},
  "A3b_decide35":     {"decide": 3.5, "strong": {}, "weak": {"tp2": 4.0, "floor": 2.0}},
  "A2b_decide35":     {"decide": 3.5, "strong": {}, "weak": {"floor": 2.0}},
  # ── B: 구조와 무관한 peak 래칫 (도달했던 최고점 기준 바닥 상향) ────────
  "B1_pk5_fl3":       {"decide": 3.0, "strong": {}, "weak": {}, "ratchet": [(5.0, 3.0)]},
  "B2_pk5_fl4":       {"decide": 3.0, "strong": {}, "weak": {}, "ratchet": [(5.0, 4.0)]},
  "B3_pk35_fl2":      {"decide": 3.0, "strong": {}, "weak": {}, "ratchet": [(3.5, 2.0)]},
  "B4_pk35_pk5":      {"decide": 3.0, "strong": {}, "weak": {},
                       "ratchet": [(3.5, 2.0), (5.0, 3.0)]},
  # ── C: 구조판정 + 래칫 ─────────────────────────────────────────────────
  "C1_A3_pk5fl3":     {"decide": 3.0, "strong": {}, "weak": {"tp2": 4.0, "floor": 2.0},
                       "ratchet": [(5.0, 3.0)]},
  "C2_A3_pk5fl3_S":   {"decide": 3.0, "strong": {}, "weak": {"tp2": 4.0, "floor": 2.0},
                       "ratchet": [(5.0, 3.0)], "ratchet_mode": "STRONG"},
  "C3_A6_pk5fl3":     {"decide": 3.0, "strong": {"tp2": 8.0}, "weak": {"tp2": 4.0, "floor": 2.0},
                       "ratchet": [(5.0, 3.0)]},
}

# ── D: 이익구간에서 구조붕괴 시 잔량 청산 (tregime.should_release C1~C4 —
#    09-17 Trend Regime Hold 연구에서 이미 정의·검증된 해제규칙 그대로) ──────
for _arm in (3.0, 3.5):
    for _v in ("C1", "C2", "C3", "C4"):
        AX[f"D_{_v}_arm{_arm}"] = {"decide": 3.0, "strong": {}, "weak": {},
                                   "brk": {"arm": _arm, "variant": _v, "mode": None}}
# WEAK 모드에서만
for _v in ("C1", "C3", "C4"):
    AX[f"D_{_v}_arm3.0_W"] = {"decide": 3.0, "strong": {}, "weak": {},
                              "brk": {"arm": 3.0, "variant": _v, "mode": "WEAK"}}
