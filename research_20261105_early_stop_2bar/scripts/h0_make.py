"""재생 하네스 생성기 — ARM3 하네스에 '초기 30분 손절 2봉 확인'(규칙 B) 모드를 덧붙인다.

기존 research_20261030_tick_arm3/scripts/h0_make.py 의 패치 목록을 그대로 쓰고
패치 2건만 더한다. production 코드는 한 줄도 바꾸지 않는다.

핵심 구현: 손절선을 다시 계산하지 않는다.
  진입 후 WK_SLHOLD_MIN 분 이내의 **첫** TIME_WINDOW_STOP_LOSS 집행만 1회 억제하면,
  production 이 다음 완성 3분봉에서 같은 래더로 **스스로 재판정**한다.
    · 다음 봉 종가도 손절선 이하 -> production 이 다시 손절을 집행한다(= CONFIRMED)
    · 손절선 위로 회복 -> production 이 애초에 청산을 내지 않는다(= SURVIVE)
  유예 중 WK_SLHOLD_HARD% 에 닿으면 틱 단위로 즉시 집행한다(run_once 앞단).

env
  WK_SLHOLD       "" / 0 = OFF(기본) · 1|ON = 시간조건만 · MU = + 보유방향 MU DIVERGE 면 유예 금지
  WK_SLHOLD_MIN   유예 허용 창(진입 후 분). 기본 30
  WK_SLHOLD_HARD  유예 중 hard stop(net %). 기본 -2.5

출력 원장에 slh(판정 로그) / slh_mode 가 추가된다.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "research_20261030_tick_arm3" / "scripts" / "h0_make.py"
DST = ROOT / "research_20261105_early_stop_2bar" / "harness" / "test_zz_slh.py"

_spec = importlib.util.spec_from_file_location("arm3_h0_make", BASE)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

# ARM_BLOCK 의 마지막 두 줄 — 여기서 _HOLD_BARS 블록이 끝난다. 그 뒤(들여쓰기 4)에 붙인다.
ANCHOR = ('        from app.trading.macd2.models import SignalState as _SSh\n'
          '        monkeypatch.setattr(_oexh, "execute_exit", _exec_exit_defer)\n')

SLH_BLOCK = '''
    # ── 2026-11-05 ④ 초기 N분 손절 2봉 확인 (규칙 B) ─────────────────────────
    # 진입 후 _SLH_MIN 분 이내의 **첫** TIME_WINDOW_STOP_LOSS 집행만 한 번 억제한다.
    # 손절선을 다시 계산하지 않는다 -- production 이 다음 완성 3분봉에서 같은
    # 래더로 스스로 재판정하므로, 아래 봉이면 다시 손절이 오고 회복하면 안 온다.
    # 유예 중에도 다른 청산 규칙은 그대로 돌고, _SLH_HARD% 에 닿으면 즉시 집행한다.
    SLHLOG = []
    _SLH = (os.environ.get("WK_SLHOLD", "") or "").upper()
    _SLH_MIN = float(os.environ.get("WK_SLHOLD_MIN", "30") or 30)
    _SLH_HARD = float(os.environ.get("WK_SLHOLD_HARD", "-2.5") or -2.5)

    if _SLH in ("1", "ON", "MU"):
        from app.trading.macd2 import order_executor as _oxs
        from app.trading.macd2 import mu_assist as _mus
        from app.trading.macd2.models import Direction as _Ds
        from app.trading.macd2.models import SignalState as _SSs
        _o_exec_s = _oxs.execute_exit
        _o_run_s = worker.run_once
        SDEF = {"epoch": None, "used": False, "until": None, "sym": None, "reason": None}

        def _slh_net(symbol, avg, qty, now):
            bid = (BOOKQ.get(symbol) or (None, None))[0] or px(symbol, now)
            if not bid or not avg or not qty:
                return None
            return worker._net_return_pct(symbol, float(avg), float(bid), int(qty))

        def _slh_mu_bad(now, symbol):
            """보유방향 MU 패턴이 DIVERGE(= 사용자 표현의 BAD) 인가. 판정 불가면 False."""
            held = _Ds.UP_RED if symbol == config.LONG_SYMBOL else _Ds.DOWN_BLUE
            try:
                ser = _mus.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                st = str(_mus.evaluate_pattern(ser, held, anchor).get("mu_pattern_state"))
            except Exception:
                return False
            return st == _mus.PATTERN_DIVERGE

        def _exec_exit_slh(*, broker, symbol, quantity, exit_reason, entry_price, **kw):
            now = clock["now"]
            st = STATE.get("s")
            ep = int(getattr(st, "position_epoch", 0) or 0) if st is not None else 0
            reason = str(exit_reason or "")
            if SDEF["epoch"] != ep:
                SDEF.update(epoch=ep, used=False, until=None, sym=None, reason=None)

            def _thru():
                return _o_exec_s(broker=broker, symbol=symbol, quantity=quantity,
                                 exit_reason=exit_reason, entry_price=entry_price, **kw)

            if reason != config.EXIT_TW_STOP_LOSS or SDEF["used"]:
                return _thru()
            pos = getattr(st, "position", None) if st is not None else None
            ent = getattr(pos, "entry_at", None) if pos is not None else None
            held_min = None if ent is None else (now - ent).total_seconds() / 60.0
            nv = _slh_net(symbol, entry_price, quantity, now)
            why = None
            if held_min is None:
                why = "NO_ENTRY_AT"
            elif held_min > _SLH_MIN:
                why = "LATE"
            elif nv is not None and nv <= _SLH_HARD:
                why = "ALREADY_HARD"
            elif _SLH == "MU" and _slh_mu_bad(now, symbol):
                why = "MU_DIVERGE"
            if why:
                SLHLOG.append(dict(k="PASS", t=now.isoformat(), sym=symbol, why=why, epoch=ep,
                                   held=(None if held_min is None else round(held_min, 1)),
                                   net=(None if nv is None else round(nv, 4))))
                return _thru()
            _b1 = worker.forming_bar_window(now)[1]
            SDEF.update(used=True, until=_b1, sym=symbol, reason=reason)
            SLHLOG.append(dict(k="DEFER", t=now.isoformat(), sym=symbol, epoch=ep,
                               until=_b1.isoformat(), held=round(held_min, 1),
                               net=(None if nv is None else round(nv, 4))))
            return _oxs.ExecutionOutcome("", _Ds.HOLD, symbol, _SSs.DETECTED,
                                         block_reason="RESEARCH_SLHOLD_DEFER")

        def _run_once_slh(**k):
            """유예 중 hard stop 만 틱 단위로 본다. 그 외에는 production 그대로."""
            now, st = k.get("now"), k.get("state")
            if SDEF["until"] is not None and st is not None and now is not None:
                pos = getattr(st, "position", None)
                ep = int(getattr(st, "position_epoch", 0) or 0)
                if pos is None or not pos.symbol or ep != SDEF["epoch"] or now >= SDEF["until"]:
                    SDEF.update(until=None)       # 유예창 종료 -> production 재판정에 맡긴다
                else:
                    nv = _slh_net(pos.symbol, pos.avg_price, pos.quantity, now)
                    if nv is not None and nv <= _SLH_HARD:
                        SLHLOG.append(dict(k="HARD", t=now.isoformat(), sym=pos.symbol,
                                           net=round(nv, 4), epoch=ep))
                        oc = _o_exec_s(
                            broker=k.get("broker"), symbol=pos.symbol, quantity=int(pos.quantity),
                            exit_reason=SDEF["reason"], entry_price=float(pos.avg_price),
                            reconcile_retries=worker.ORDER_FILL_RECONCILE_RETRIES,
                            reconcile_delay_sec=worker.ORDER_FILL_RECONCILE_DELAY_SEC)
                        worker._apply_exit_outcome(st, oc, exit_reason=SDEF["reason"])
                        if oc.final_state == _SSs.EXECUTED:
                            st.time_window_position_active = False
                        SDEF.update(until=None)
            return _o_run_s(**k)

        monkeypatch.setattr(_oxs, "execute_exit", _exec_exit_slh)
        monkeypatch.setattr(worker, "run_once", _run_once_slh)
'''

EXTRA = [
    (ANCHOR, ANCHOR + SLH_BLOCK),
    ("hold=HOLDLOG, hold_kind=_HOLD, ",
     "hold=HOLDLOG, hold_kind=_HOLD, slh=SLHLOG, slh_mode=_SLH, "),
]


def main() -> None:
    text = base.SRC.read_text(encoding="utf-8")
    for old, new in base.PATCHES + [base.BIND_BOOK] + EXTRA:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"!! 패치 대상이 {n}개 (1개여야 함): {old[:80]!r}")
        text = text.replace(old, new, 1)
    head = ('"""[2026-11-05 초기 30분 손절 2봉 확인(규칙 B) 재생 연구 사본]\\n'
            'ARM3 하네스(research_20261030_tick_arm3) 위에 WK_SLHOLD 모드만 더했다.\\n'
            'production 무수정. 자세한 내용은 research_20261105_early_stop_2bar/scripts/h0_make.py.\\n\\n'
            '── 이하 원본 docstring ──\\n')
    text = head + text[len('"""'):]
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(text, encoding="utf-8")
    print(f"-> {DST}  ({len(text):,}자, 패치 {len(base.PATCHES) + 1 + len(EXTRA)}건)")


if __name__ == "__main__":
    main()
