"""MU 원신호 하네스 생성 — ARM3 하네스 사본에 진입원(原信號) 교체 블록 하나만 덧붙인다.

production 무수정. 바꾸는 것은 **진입을 누가 일으키는가** 하나뿐이다.

WK_MUPRI
  ""/0 : 원본 ARM3 그대로
  M1   : MU 원신호 진입 + 하이닉스 플래그 게이트 **우회**
         (TW2 T+3 확인 / TW2 추가 veto / TEG / 3번째슬롯 품질 / Slot1 CHOP veto)
  M2   : MU 원신호 진입 + 위 하이닉스 게이트 **그대로 유지** (HY 구조가 얹히는 효과 측정용)

두 변형 모두 아래는 **현행 그대로**다:
  슬롯/하루한도(resolve_slot·daily_cap) · 시간창 · CHOP MC(morning_no_trade) · 사이징
  TP/SL/트레일링/B3/H50/C1/강제청산 · 반대 하이닉스 플래그의 청산 경로

하이닉스 플래그는 **진입만** 막는다(MUPRI_HY_ENTRY_OFF). 플래그 자체는 그대로 생성되므로
H50/B3/반대신호 청산 경로는 살아 있다.

MU 진입 시점은 외부 plan 파일(MUPRI_PLAN)이 준다 — m4_plan.py 가 MU 1분봉 플래그 +
mu_assist.evaluate_pattern + exec_strength 로 미리 계산한 결정시각 목록이다(미래 데이터 없음).
판정 로그: WK_OUT + ".mupri.json"
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "research_20261030_tick_arm3" / "harness" / "test_zz_arm3.py"
DST = ROOT / "research_20261104_mu_primary" / "harness" / "test_zz_mupri.py"

ANCHOR = '    monkeypatch.setattr(worker, "_git_sha", lambda: "replay")\n'
PATCH = ANCHOR + '''
    # ── MU 원신호 진입 (2026-11-04 연구, research only) ───────────────────────
    _MUPRI = (os.environ.get("WK_MUPRI", "") or "").upper()
    MUPRILOG = []
    if _MUPRI in ("M1", "M2"):
        import dataclasses as _dcm
        from datetime import timedelta as _tdm
        from app.trading.macd2 import teg_gate as _tegm
        from app.trading.macd2 import time_window_3slot as _t3m
        from app.trading.macd2 import time_window_filter as _twfm
        from app.trading.macd2.models import Direction as _Dm
        from app.trading.macd2.signal_engine import make_signal_id as _mkid

        _plan_all = json.loads(Path(os.environ["MUPRI_PLAN"]).read_text(encoding="utf-8"))
        _plan = sorted(_plan_all.get(os.environ["WK_D"], []), key=lambda x: x["decide"])
        _MU = {"in": False, "fired": set()}

        _o_mres = worker._resolve_tw2_3slot_candidate

        def _mres(**kw):
            st, now, snap = kw["state"], kw["now"], kw["macd_snap"]
            _nk = now.astimezone(config.KST)
            # 재생은 전일(P)·전전일(P2)을 먼저 돌린다 — **그 날엔 절대 주입하지 않는다**
            if (snap is not None and _nk.strftime("%Y%m%d") == os.environ["WK_D"]
                    and not st.tw2_3slot_pending_flag_direction
                    and not st.time_window_pending_flag_direction):
                hhmm = _nk.strftime("%H:%M")
                for p in _plan:
                    if p["key"] in _MU["fired"] or p["decide"] > hhmm:
                        continue
                    d = _Dm(p["dir"])
                    fb = None
                    for back in range(1, 6):      # signal_id 충돌 회피 (HY 플래그와 같은 봉일 때)
                        cand = snap.bar_dt - _tdm(minutes=3 * back)
                        if f"{_mkid(cand, d)}:TW2_3SLOT_CONFIRM" not in st.processed_signal_ids:
                            fb = cand
                            break
                    _MU["fired"].add(p["key"])
                    if fb is None:
                        MUPRILOG.append(dict(t=now.isoformat(), skip="SIGNAL_ID_COLLISION", **p))
                        break
                    st.tw2_3slot_pending_flag_direction = d
                    st.tw2_3slot_pending_flag_bar_ts = fb.isoformat()
                    MUPRILOG.append(dict(t=now.isoformat(), fired=True, flag_bar=fb.isoformat(), **p))
                    _MU["in"] = True
                    break
            try:
                return _o_mres(**kw)
            finally:
                _MU["in"] = False
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _mres)

        # 진입 게이트: MU 원신호면 통과, 하이닉스 플래그면 진입 차단(청산 경로는 그대로)
        _o_mgate = _twfm.evaluate_time_window_entry

        def _mgate(bars_3m, fdir, fbar, dat, **kw):
            dec = _o_mgate(bars_3m, fdir, fbar, dat, **kw)
            if _MU["in"]:
                return _dcm.replace(dec, approved=True, decision="MUPRI_APPROVED", block_reason=None)
            # 하이닉스 플래그 진입 차단. 거절이어도 사유가 TW_REJECT_TIME_WINDOW 면
            # 호출부의 window_blocked_by_morning_only 가 오후 후보를 그대로 통과시키므로
            # 그 사유도 같이 바꿔야 샌 길이 막힌다(2026-11-04 실측으로 확인한 누수).
            if dec.approved or dec.block_reason == config.TW_REJECT_TIME_WINDOW:
                return _dcm.replace(dec, approved=False, decision="MUPRI_HY_ENTRY_OFF",
                                    block_reason="MUPRI_HY_ENTRY_OFF")
            return dec
        monkeypatch.setattr(_twfm, "evaluate_time_window_entry", _mgate)

        if _MUPRI == "M1":
            _o_mveto = _twfm.evaluate_tw2_extra_vetoes
            monkeypatch.setattr(_twfm, "evaluate_tw2_extra_vetoes",
                                lambda *a, **k: (False, None) if _MU["in"] else _o_mveto(*a, **k))
            _o_mteg = _tegm.evaluate_teg
            monkeypatch.setattr(_tegm, "evaluate_teg", lambda *a, **k: (
                _dcm.replace(_o_mteg(*a, **k), approved=True, reject_reasons=())
                if _MU["in"] else _o_mteg(*a, **k)))
            _o_mq = _t3m.evaluate_trend_quality
            monkeypatch.setattr(_t3m, "evaluate_trend_quality", lambda *a, **k: (
                _dcm.replace(_o_mq(*a, **k), approved=True) if _MU["in"] else _o_mq(*a, **k)))
            _o_ms1 = _t3m.evaluate_slot1_chop_veto
            monkeypatch.setattr(_t3m, "evaluate_slot1_chop_veto", lambda *a, **k: (
                _dcm.replace(_o_ms1(*a, **k), vetoed=False) if _MU["in"] else _o_ms1(*a, **k)))

    def _mupri_dump():
        Path(os.environ["WK_OUT"] + ".mupri.json").write_text(
            json.dumps(dict(mode=_MUPRI, log=MUPRILOG), ensure_ascii=False, default=str),
            encoding="utf-8")
    import atexit as _atexit
    _atexit.register(_mupri_dump)
'''


def main():
    s = SRC.read_text(encoding="utf-8")
    assert s.count(ANCHOR) == 1, "anchor 가 정확히 1개가 아니다"
    s = s.replace(ANCHOR, PATCH)
    out_anchor = '    Path(os.environ["WK_OUT"]).write_text('
    assert s.count(out_anchor) == 1
    s = s.replace(out_anchor, '    _mupri_dump()\n' + out_anchor)
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(s, encoding="utf-8", newline="\n")
    print("written", DST)


if __name__ == "__main__":
    main()
