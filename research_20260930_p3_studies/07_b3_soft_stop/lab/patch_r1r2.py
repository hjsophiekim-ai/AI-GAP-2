"""2026-09-30 연구 훅 패치 (R1 profit-lock / R2 reverse guard + 진단).

READ-ONLY 연구. production 무수정 -- lab/hengine5.py (연구 엔진 사본)에만 적용한다.
모든 훅은 opt-in (b3["r1"] / b3["r2"]) 이고, 진단 필드는 기록만 하므로 R0 결과를 바꾸지 않는다
(패치 후 R0 parity 로 확인).
"""
from pathlib import Path

P = Path(__file__).resolve().parent / "hengine5.py"
src = P.read_text(encoding="utf-8")
assert "2026-09-30 연구 훅" not in src, "already patched"


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (n, old[:80])
    src = src.replace(old, new)


# 1) Trade 진단 필드
rep("""    b3_late_at: Optional[str] = None
""", """    b3_late_at: Optional[str] = None
    # 2026-09-30 연구 훅 필드
    p2_at: Optional[str] = None         # net +2.0% 최초 도달(틱/완성봉)
    p2_floor_at: Optional[str] = None   # p2 이후 완성봉 net <= +1.0% 최초
    r1_fired: bool = False
    r1_fire_at: Optional[str] = None
    r2_deferred: int = 0
    r2_ignored: int = 0
    opp: list = field(default_factory=list)   # 보유 중 반대플래그 판정 로그
""")

# 2) 상태 변수
rep("""    _pm1_defer = None
    slots_used_today""", """    _pm1_defer = None
    r2p = None                        # 2026-09-30 연구 훅 R2: (dir, p_idx, p_bar_ts, defer_idx)
    slots_used_today""")
rep("""                pending = None
                whipsaw_watch = None
                hold = None
                rhold = None
                w1a_seq = 0""", """                pending = None
                r2p = None
                whipsaw_watch = None
                hold = None
                rhold = None
                w1a_seq = 0""")

# 3) pending 블록 앞: R2 유예 재판정
rep("""            if pending is not None:
                p_direction, p_idx, p_bar_ts = pending
                if idx == p_idx + 1:
                    pending = None""", """            # ── 2026-09-30 연구 훅 R2: 1봉 유예 후 재판정 ──
            r2_force = False
            if r2p is not None and idx == r2p[3] + 1:
                _r2d, _r2i, _r2t, _r2k = r2p
                r2p = None
                _newflag = pending is not None and pending[1] == idx - 1
                if position is not None and position["symbol"] != order_executor.target_symbol_for_direction(_r2d) \\
                        and trend_ok_at(idx, position["symbol"], True):
                    position["rec"].r2_ignored += 1
                    position["rec"].opp.append({"at": (bar_start + timedelta(minutes=3)).isoformat(),
                                                "idx": int(idx), "r2": "IGNORED"})
                elif _newflag:
                    pass   # 유예봉에 새 플래그 -- 새 플래그가 우선 (원 판정 폐기)
                else:
                    pending = (_r2d, _r2i, _r2t)
                    r2_force = True
                    if position is not None:
                        position["rec"].opp.append({"at": (bar_start + timedelta(minutes=3)).isoformat(),
                                                    "idx": int(idx), "r2": "FORCED"})
            if pending is not None:
                p_direction, p_idx, p_bar_ts = pending
                if idx == p_idx + 1 or r2_force:
                    pending = None""")

# 4) 반대신호 분기: 진단 + R2 유예
rep("""                    if regime_handled or h50_handled:
                        pass
                    elif not final_approved:""", """                    # ── 2026-09-30 연구 훅: 반대플래그 진단 + R2 ──
                    _r2_defer = False
                    if b3 is not None and position is not None and position["symbol"] != target:
                        _hd_dir = bt._direction_for_symbol(position["symbol"])
                        _hd = _memo("h50h", (idx, _hd_dir.value),
                                    lambda: swh.evaluate_hold(bars_slice, _hd_dir, recognition_at))
                        _pb = position.get("b3")
                        _rr = position["rec"]
                        _is_runner = bool(_pb is not None and _pb["promoted"]
                                          and (_rr.b3_rescued or _rr.b3_ext_prom))
                        _kind = ("P3-RUNNER" if _is_runner else
                                 "Y3-RUNNER" if (_pb is not None and _pb["promoted"]) else
                                 "B3" if _pb is not None else "BASE")
                        _n1t = trend_ok_at(idx, position["symbol"], True)
                        _pxn = fill_at(position["symbol"], recognition_at)
                        _dg = {"at": recognition_at.isoformat(), "idx": int(idx), "flag_idx": int(p_idx),
                               "opp": p_direction.value, "held": _hd_dir.value, "kind": _kind,
                               "approved": bool(final_approved), "reason": str(final_reason),
                               "h50_on": bool(h50_on(position)), "h50_hold": bool(_hd.should_hold),
                               "h50_reason": _hd.reason, "h50_trend": _hd.trend,
                               "range_pct": _hd.range_pct, "n1_trend": bool(_n1t),
                               "net": (None if _pxn is None else round(float(net_at(_pxn)), 4)),
                               "forced": bool(r2_force)}
                        if (b3.get("r2") and not r2_force and not h50_handled and not regime_handled
                                and final_approved and _is_runner and _n1t):
                            _r2_defer = True
                            r2p = (p_direction, p_idx, p_bar_ts, idx)
                            _rr.r2_deferred += 1
                            _dg["r2"] = "DEFER"
                        _rr.opp.append(_dg)
                    if regime_handled or h50_handled or _r2_defer:
                        pass
                    elif not final_approved:""")

# 5) 틱 루프: +2% 최초 도달 진단
rep("""                    note_ms(net, tick, idx)
""", """                    note_ms(net, tick, idx)
                    if position["rec"].p2_at is None and net >= 2.0 - 1e-12:
                        position["rec"].p2_at = pd.Timestamp(tick).isoformat()
""")

# 6) 완성봉 래더: 진단 + R1
rep("""                    note_ms(net, recognition_at, idx)
""", """                    note_ms(net, recognition_at, idx)
                    if position["rec"].p2_at is None and net >= 2.0 - 1e-12:
                        position["rec"].p2_at = pd.Timestamp(recognition_at).isoformat()
                    if (position["rec"].p2_at is not None and position["rec"].p2_floor_at is None
                            and net <= 1.0 + 1e-12):
                        position["rec"].p2_floor_at = pd.Timestamp(recognition_at).isoformat()
""")
rep("""                    if position is not None and position["ax"].get("close"):
                        close_trade(recognition_at, px, "AX_LOCK_EXIT", idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
""", """                    if position is not None and position["ax"].get("close"):
                        close_trade(recognition_at, px, "AX_LOCK_EXIT", idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
                    # ── 2026-09-30 연구 훅 R1: P3-RUNNER profit lock (opt-in) ──
                    #    ARM = MFE >= arm (틱/완성봉), EXIT = ARM 이후 완성봉 net <= floor
                    #    대상 = Q2/H30 부분익절로 승격된 P3-RUNNER 만 (Y3-RUNNER 제외)
                    _r1b = position.get("b3")
                    if (b3 is not None and b3.get("r1") and _r1b is not None and _r1b["promoted"]
                            and (position["rec"].b3_rescued or position["rec"].b3_ext_prom)
                            and float(position["rec"].peak_net_pct) >= float(b3["r1"]["arm"]) - 1e-12
                            and net <= float(b3["r1"]["floor"]) + 1e-12):
                        position["rec"].r1_fired = True
                        position["rec"].r1_fire_at = pd.Timestamp(recognition_at).isoformat()
                        close_trade(recognition_at, px, "R1_PROFIT_LOCK", idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
""")

P.write_text(src, encoding="utf-8")
print("patched OK")
