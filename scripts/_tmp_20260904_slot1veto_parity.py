"""READ-ONLY 패리티 검증 (2026-09-04): production 에 넣은
``time_window_3slot.evaluate_slot1_chop_veto`` 가 검증된 백테스트 C 안과
**같은 후보를 차단**하는지 결정 단위로 대조한다.

체인을 다시 복제하지 않는다. 대신 이미 검증된 C 체인 산출물
(``data/validation/lossveto_fullchain/raw.json``)에서
  - C 가 차단한 Slot1 CHOP 후보 9건        -> production 헬퍼도 vetoed=True 여야
  - C 가 실제 체결한 Slot1 진입 전부        -> production 헬퍼는 vetoed=False 여야
  - Slot2/Slot3 진입 전부                   -> applicable=False (판정 자체 안 함)
  - 토글 OFF                                -> 전 건 vetoed=False, applicable=False
를 확인한다. bars 프레임/시각은 worker 가 넘기는 것과 동일하게 T+3 확정봉까지
truncate 한 프레임과 그 봉의 마감시각을 쓴다.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

import _tmp_20260904_runner_dataset as ds  # noqa: E402

RAW = PROJECT_ROOT / "data" / "validation" / "lossveto_fullchain" / "raw.json"


def _dir(v: str) -> Direction:
    return Direction.UP_RED if v == "UP_RED" else Direction.DOWN_BLUE


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    bars = ctx.hynix_bars_3m
    raw = json.loads(RAW.read_text(encoding="utf-8"))

    def call(decision_idx: int, direction: str, slot_number, enabled=None):
        sl = bars.iloc[: decision_idx + 1]
        at = pd.Timestamp(bars["datetime"].iloc[decision_idx]).to_pydatetime() \
            + timedelta(minutes=3)
        return tw3.evaluate_slot1_chop_veto(
            sl, _dir(direction), at, slot_number=slot_number, enabled=enabled)

    ok = True

    # 1) C 가 차단한 9건 -> production 헬퍼도 vetoed=True
    blocked = raw["blocked_C"]
    bad = []
    for b in blocked:
        d = call(b["decision_idx"], b["direction"], 1)
        if not (d.vetoed and d.applicable and d.is_chop):
            bad.append((b["date"], b["decision_at"], d.vetoed, d.is_chop))
    print(f"[1] C 차단 {len(blocked)}건 -> production vetoed=True : "
          f"{'PASS' if not bad else 'FAIL ' + str(bad)}")
    ok &= not bad

    # 2) C 가 실제 체결한 Slot1 진입 -> vetoed=False
    slot1_entries = [t for t in raw["C"] if t["slot_number"] == 1]
    bad = []
    for t in slot1_entries:
        d = call(t["decision_idx"], t["direction"], 1)
        if d.vetoed:
            bad.append((t["date"], t["entry_time"]))
    print(f"[2] C 체결 Slot1 {len(slot1_entries)}건 -> vetoed=False : "
          f"{'PASS' if not bad else 'FAIL ' + str(bad)}")
    ok &= not bad

    # 3) Slot2/Slot3 진입 -> applicable=False (CHOP 판정 자체를 안 함)
    others = [t for t in raw["C"] if t["slot_number"] in (2, 3)]
    bad = []
    for t in others:
        d = call(t["decision_idx"], t["direction"], t["slot_number"])
        if d.applicable or d.vetoed:
            bad.append((t["date"], t["slot_number"]))
    n_chop = sum(1 for t in others if t["entry_chop"])
    print(f"[3] Slot2/3 진입 {len(others)}건(그중 entry_chop=True {n_chop}건) -> "
          f"applicable=False : {'PASS' if not bad else 'FAIL ' + str(bad)}")
    ok &= not bad

    # 4) 토글 OFF -> 전 건 vetoed=False / applicable=False
    bad = []
    for b in blocked:
        d = call(b["decision_idx"], b["direction"], 1, enabled=False)
        if d.vetoed or d.applicable:
            bad.append(b["date"])
    print(f"[4] 토글 OFF -> 차단 0건 : {'PASS' if not bad else 'FAIL ' + str(bad)}")
    ok &= not bad

    # 5) A 대비 차단 대상이 정확히 C 의 것과 일치 (A 의 Slot1 진입 전수 스캔)
    a_slot1 = [t for t in raw["A"] if t["slot_number"] == 1]
    veto_hits = {(t["decision_idx"], t["direction"]) for t in a_slot1
                 if call(t["decision_idx"], t["direction"], 1).vetoed}
    c_blocked_keys = {(b["decision_idx"], b["direction"]) for b in blocked}
    # C 는 슬롯 미소비라 A 에 없던 후보(20260813 12:00)도 차단하므로 A∩ 로 비교
    inter = c_blocked_keys & {(t["decision_idx"], t["direction"]) for t in a_slot1}
    same = veto_hits == inter
    print(f"[5] A 의 Slot1 진입 {len(a_slot1)}건 스캔 -> veto 대상 {len(veto_hits)}건, "
          f"C 차단분과 일치(A 교집합 {len(inter)}건): {'PASS' if same else 'FAIL'}")
    if not same:
        print("    only-in-scan:", sorted(veto_hits - inter))
        print("    only-in-C   :", sorted(inter - veto_hits))
    ok &= same

    print("\nPARITY: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
