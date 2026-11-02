"""REGIME_E 하네스 생성 — ARM3 하네스 사본에 E 대기 우회 1개만 덧붙인다 (production 무수정).

WK_REGIME_E
  0 / 미설정 : A (현행, 원본과 동일 동작)
  1          : B — 승인 시점 state.p3_last_regime == "TREND" 이면 E 돌파대기를 건너뛰고
               T+3 즉시진입 경로로 보낸다. CHOP / WARMUP 은 현행 E 그대로.
  FORCE      : 비교용 — regime 무관 E 대기 전면 생략 (11/02 재현처럼 재생 regime 이
               실거래와 다를 때 '그 진입이 TREND 였다면' 을 보기 위한 용도)
판정 로그: WK_OUT + ".rege.json"
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "research_20261030_tick_arm3" / "harness" / "test_zz_arm3.py"
DST = ROOT / "research_20261102_reclaim" / "harness" / "test_zz_rege.py"

ANCHOR = '    monkeypatch.setattr(worker, "_git_sha", lambda: "replay")\n'
PATCH = ANCHOR + '''
    # ── REGIME_E (2026-11-02 연구) — E 돌파대기를 regime 별로 분리 ──────────────
    _REGE = (os.environ.get("WK_REGIME_E", "0") or "0").upper()
    REGELOG = []
    if _REGE in ("1", "FORCE"):
        _o_earm = worker._e_arm_pending_instead_of_entry

        def _rege_arm(**kw):
            st = kw.get("state")
            reg = str(getattr(st, "p3_last_regime", None) or "")
            skip = (_REGE == "FORCE") or (reg == "TREND")
            REGELOG.append(dict(t=str(kw.get("now")), sid=str(kw.get("signal_id")), regime=reg,
                                skip=bool(skip), price=kw.get("price"),
                                trigger=getattr(kw.get("early"), "trigger", None)))
            if skip:
                return False          # 대기 안 함 -> 호출부가 T+3 즉시진입으로 진행
            return _o_earm(**kw)
        monkeypatch.setattr(worker, "_e_arm_pending_instead_of_entry", _rege_arm)

    def _rege_dump():
        Path(os.environ["WK_OUT"] + ".rege.json").write_text(
            json.dumps(dict(mode=_REGE, log=REGELOG), ensure_ascii=False, default=str), encoding="utf-8")
    import atexit as _atexit
    _atexit.register(_rege_dump)
'''


def main():
    s = SRC.read_text(encoding="utf-8")
    assert s.count(ANCHOR) == 1, "anchor 가 정확히 1개가 아니다"
    s = s.replace(ANCHOR, PATCH)
    # 출력 직전에도 한 번 기록 (atexit 는 보험)
    out_anchor = '    Path(os.environ["WK_OUT"]).write_text('
    assert s.count(out_anchor) == 1
    s = s.replace(out_anchor, '    _rege_dump()\n' + out_anchor)
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(s, encoding="utf-8", newline="\n")
    print("written", DST)


if __name__ == "__main__":
    main()
