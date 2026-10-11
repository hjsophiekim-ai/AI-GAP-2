"""2026-09-30 (7차) 연구 훅: B3 SOFT STOP (opt-in b3["soft"] = {"hard": 1.3}).

READ-ONLY 연구. lab/hengine5.py 사본에만 적용. B3 가 관리하는(승격 전) CHOP 포지션에만.
- tick net <= -SL(1.0) 최초 도달 시: 직전 완성봉 기준 보유방향 N1 추세 유지 ∧ MACD gap 이 보유 반대쪽에서 확대 중이 아님
  -> SOFT (즉시 손절 안 함). 아니면 기존대로 GX_SL.
- SOFT 중 tick net <= -hard(1.3) -> 즉시 전량 GX_HARD.
- SOFT 를 건 봉이 완성되는 시점(= 다음 완성 3분봉, 유예 1~3분)에 같은 구조 판정:
    유지 -> SOFT 해제, 기존 B3/Q2/H30/Y3 복귀 (포지션당 SOFT 1회, 이후 -1% 는 즉시 손절)
    붕괴 -> 그 봉 인식시각 가격으로 전량 GX_SOFT_SL
"""
from pathlib import Path

P = Path(__file__).resolve().parent / "hengine5.py"
src = P.read_text(encoding="utf-8")
assert "연구 훅 SOFT" not in src, "already patched"


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (n, old[:90])
    src = src.replace(old, new)


rep("""    reent_orig_entry: Optional[str] = None
""", """    reent_orig_entry: Optional[str] = None
    soft_at: Optional[str] = None        # 연구 훅 SOFT: -1% 최초 도달(유예 시작) 시각
    soft_trend: Optional[bool] = None    # 도달 직전 완성봉 N1 추세
    soft_against: Optional[bool] = None  # 도달 직전 MACD gap 반대방향 확대
    soft_bar_trend: Optional[bool] = None
    soft_bar_against: Optional[bool] = None
    soft_result: str = ""                # NO_SOFT / RECOVER / BAR_SL / HARD
""")

# 구조 판정 helper (run_chain 안, trend_ok_at 옆)
rep("""    def eff_cfg(trend_, cfg_):""", """    def _soft_state(ci_, symbol_):
        # 연구 훅 SOFT: 완성봉 ci_ 기준 (보유방향 N1 추세, MACD gap 반대방향 확대)
        held_ = bt._direction_for_symbol(symbol_)
        trend_ = bool(tr.snapshot(bars.iloc[: ci_ + 1], held_).ok) if ci_ >= 1 else False
        sg_ = 1.0 if symbol_ == config.LONG_SYMBOL else -1.0
        against_ = False
        if ci_ >= 1 and (pd.Timestamp(bars["datetime"].iloc[ci_]).date()
                         == pd.Timestamp(bars["datetime"].iloc[ci_ - 1]).date()):
            against_ = bool(sg_ * _b3_hist[ci_] < 0 and sg_ * (_b3_hist[ci_] - _b3_hist[ci_ - 1]) < 0)
        return trend_, against_

    def eff_cfg(trend_, cfg_):""")

# 틱 손절 자리
rep("""                        if net <= _slv:
                            close_trade(tick, price, "GX_SL" if _slv <= -float(b3["sl"]) + 1e-12 else "GX_BE", idx)""",
    """                        # ── 연구 훅 SOFT: hard stop / soft 진입 ──
                        _sft = _bs.get("soft")
                        if _sft is not None and net <= -float(b3["soft"]["hard"]) + 1e-12:
                            _rec.soft_result = "HARD"
                            close_trade(tick, price, "GX_HARD", idx)
                            position = None; whipsaw_watch = None; hold = _clr(hold); rhold = None
                            break
                        if _sft is not None:
                            _slv = -1e9   # 유예 중에는 -1% 손절 안 함 (hard 만)
                        elif (b3.get("soft") and net <= _slv and not _bs.get("soft_used")
                              and _slv <= -float(b3["sl"]) + 1e-12):
                            _tns_s = pd.Timestamp(tick - timedelta(minutes=3)).as_unit("ns").value
                            _ci_s = int(np.searchsorted(_b3_start_ns, _tns_s, side="right")) - 1
                            _t_s, _a_s = _soft_state(_ci_s, position["symbol"])
                            _rec.soft_at = pd.Timestamp(tick).isoformat()
                            _rec.soft_trend = _t_s
                            _rec.soft_against = _a_s
                            if _t_s and not _a_s:
                                _bs["soft"] = {"idx": idx}
                                _bs["soft_used"] = True
                                continue
                            _rec.soft_result = "NO_SOFT"
                        if net <= _slv:
                            close_trade(tick, price, "GX_SL" if _slv <= -float(b3["sl"]) + 1e-12 else "GX_BE", idx)""")

# 완성봉: soft 판정
rep("""            # 완성봉 래더
""", """            # ── 연구 훅 SOFT: 유예 봉 완성 시 구조 판정 ──
            if (position is not None and position.get("b3") is not None
                    and position["b3"].get("soft") is not None and position["b3"]["soft"]["idx"] == idx):
                _pxs = fill_at(position["symbol"], recognition_at)
                _tb, _ab = _soft_state(idx, position["symbol"])
                position["rec"].soft_bar_trend = _tb
                position["rec"].soft_bar_against = _ab
                position["b3"]["soft"] = None
                if _tb and not _ab:
                    position["rec"].soft_result = "RECOVER"
                elif _pxs is not None:
                    position["rec"].soft_result = "BAR_SL"
                    close_trade(recognition_at, _pxs, "GX_SOFT_SL", idx)
                    position = None; whipsaw_watch = None; hold = _clr(hold); rhold = None
            # 완성봉 래더
""")

P.write_text(src, encoding="utf-8")
print("patched OK")
