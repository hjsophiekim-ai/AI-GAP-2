"""2026-09-30 (2차) 연구 훅: B EXIT-ONLY THEN CONFIRM / C 부분 profit lock.

READ-ONLY 연구. lab/hengine5.py (연구 엔진 사본)에만 적용. 둘 다 opt-in:
  b3["exo"]   = True                 -> 후보 B
  b3["plock"] = {"arm", "floor", "frac"} -> 후보 C30/C50
patch_r1r2.py (R1/R2 훅) 가 먼저 적용돼 있어야 한다.
"""
from pathlib import Path

P = Path(__file__).resolve().parent / "hengine5.py"
src = P.read_text(encoding="utf-8")
assert "2026-09-30 연구 훅 R2" in src, "patch_r1r2 first"
assert "연구 훅 EXO" not in src, "already patched"


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (n, old[:90])
    src = src.replace(old, new)


# 필드
rep("""    opp: list = field(default_factory=list)   # 보유 중 반대플래그 판정 로그
""", """    opp: list = field(default_factory=list)   # 보유 중 반대플래그 판정 로그
    exo_exit: bool = False            # 연구 훅 EXO: 청산만 하고 반대진입을 유예한 거래
    exo_result: str = ""              # ENTERED / CANCELLED / NEWFLAG
    exo_entry: bool = False           # 연구 훅 EXO: 1봉 유예 뒤 들어간 반대 ETF 거래
    plock_at: Optional[str] = None    # 연구 훅 PLOCK: 부분 보호 실행 시각
    plock_net: Optional[float] = None
""")

# 상태
rep("""    r2p = None                        # 2026-09-30 연구 훅 R2: (dir, p_idx, p_bar_ts, defer_idx)
""", """    r2p = None                        # 2026-09-30 연구 훅 R2: (dir, p_idx, p_bar_ts, defer_idx)
    exop = None                       # 연구 훅 EXO: (dir, p_idx, p_bar_ts, defer_idx, exited_rec)
    exo_force = False
""")
rep("""                pending = None
                r2p = None
""", """                pending = None
                r2p = None
                exop = None
""")

# 유예 재판정 (R2 블록 바로 뒤, pending 블록 앞)
rep("""            if pending is not None:
                p_direction, p_idx, p_bar_ts = pending
                if idx == p_idx + 1 or r2_force:""", """            # ── 연구 훅 EXO: 청산 후 1봉 뒤 반대방향 N1 추세 확인 ──
            exo_force = False
            if exop is not None and idx == exop[3] + 1:
                _xd, _xi, _xt, _xk, _xrec = exop
                exop = None
                _xnew = pending is not None and pending[1] == idx - 1
                if _xnew:
                    _xrec.exo_result = "NEWFLAG"
                elif position is None and tr.snapshot(bars.iloc[: idx + 1], _xd).ok:
                    pending = (_xd, _xi, _xt)
                    exo_force = True
                    _xrec.exo_result = "ENTERED"
                else:
                    _xrec.exo_result = "CANCELLED"
            if pending is not None:
                p_direction, p_idx, p_bar_ts = pending
                if idx == p_idx + 1 or r2_force or exo_force:""")

# 판정 분기: EXO
rep("""                    if regime_handled or h50_handled or _r2_defer:
                        pass
                    elif not final_approved:""", """                    # ── 연구 훅 EXO: P3-RUNNER + 승인 반대플래그 + H50 미보류 -> 청산만 ──
                    _exo = False
                    if (b3 is not None and b3.get("exo") and not exo_force and not r2_force
                            and not regime_handled and not h50_handled and not _r2_defer
                            and final_approved and position is not None and position["symbol"] != target
                            and position.get("b3") is not None and position["b3"]["promoted"]
                            and (position["rec"].b3_rescued or position["rec"].b3_ext_prom)):
                        _exo = True
                        cn = fill_at(position["symbol"], recognition_at)
                        if cn is None:
                            cn = position["rec"].entry_price
                        _xr = position["rec"]
                        _xr.exo_exit = True
                        if _xr.opp:
                            _xr.opp[-1]["exo"] = "EXIT_ONLY"
                        close_trade(recognition_at, cn, config.EXIT_OPPOSITE_SIGNAL, idx)
                        position = None
                        hold = _clr(hold)
                        rhold = None
                        whipsaw_watch = None
                        exop = (p_direction, p_idx, p_bar_ts, idx, _xr)
                    if regime_handled or h50_handled or _r2_defer or _exo:
                        pass
                    elif not final_approved:""")

# EXO 진입 표시
rep("""                                rec.day_seq = int(day_entry_seq)
""", """                                rec.day_seq = int(day_entry_seq)
                                rec.exo_entry = bool(exo_force)
""")

# PLOCK (R1 훅 바로 앞)
rep("""                    # ── 2026-09-30 연구 훅 R1: P3-RUNNER profit lock (opt-in) ──""", """                    # ── 연구 훅 PLOCK: P3-RUNNER 부분 보호 (1회) ──
                    _plb = position.get("b3")
                    _pl = (b3 or {}).get("plock")
                    if (_pl and _plb is not None and _plb["promoted"]
                            and (position["rec"].b3_rescued or position["rec"].b3_ext_prom)
                            and not position.get("plock_done")
                            and float(position["rec"].peak_net_pct) >= float(_pl["arm"]) - 1e-12
                            and net <= float(_pl["floor"]) + 1e-12):
                        _q = position["qty_frac"] * float(_pl["frac"])
                        position["realized"] += _q * net
                        position["legs"].append((ce._fmt(recognition_at), px, round(_q, 6),
                                                 "PLOCK_PARTIAL_EXIT", round(net, 4)))
                        position["qty_frac"] -= _q
                        position["plock_done"] = True
                        position["rec"].plock_at = pd.Timestamp(recognition_at).isoformat()
                        position["rec"].plock_net = round(float(net), 4)
                    # ── 2026-09-30 연구 훅 R1: P3-RUNNER profit lock (opt-in) ──""")

P.write_text(src, encoding="utf-8")
print("patched OK")
