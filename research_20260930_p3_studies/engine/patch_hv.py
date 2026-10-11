"""2026-09-30 (8차) HIGH-VOL DAY MODE 훅. READ-ONLY 연구 (lab/hengine5.py 사본만).

b3["hv_on"]  = {date: "HH:MM"}  (hv_detect.py D1 탐지기 ON 시각)
b3["hvp"]    = True -> HV 활성 중 모든 포지션에 조기익절(production 값: MFE>=+1.5 ARM, 완성봉 net<=+0.8 청산)
b3["exo_hvday"] = True + exo/exo_all + exo_confirm="GAP"
             -> HV 활성 중 승인된 반대전환은 청산만, 반대 ETF 는 1봉 뒤 MACD gap 이 새 방향 쪽 ∧ 확대일 때만 진입
"""
from pathlib import Path

P = Path(__file__).resolve().parent / "hengine5.py"
src = P.read_text(encoding="utf-8")
assert "연구 훅 SOFT" in src and "연구 훅 HVDAY" not in src


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (n, old[:90])
    src = src.replace(old, new)


rep("""    def eff_cfg(trend_, cfg_):""", """    def _hv_active(when_):
        # 연구 훅 HVDAY
        _hvo = (b3 or {}).get("hv_on")
        if not _hvo:
            return False
        _d = current_day
        return _d in _hvo and pd.Timestamp(when_).astimezone(KST).strftime("%H:%M") >= _hvo[_d]

    def eff_cfg(trend_, cfg_):""")

# EXO 조건: HV 일 활성일 때만
rep("""                            and (b3.get("exo_hv") is None
                                 or (_hd.range_pct is not None and float(_hd.range_pct) > float(b3["exo_hv"])))):""",
    """                            and (b3.get("exo_hv") is None
                                 or (_hd.range_pct is not None and float(_hd.range_pct) > float(b3["exo_hv"])))
                            # 연구 훅 HVDAY: exo_hvday 면 HIGH-VOL 일 활성 중에만
                            and (not b3.get("exo_hvday") or _hv_active(recognition_at))):""")

# EXO 1봉 뒤 확인: GAP 모드
rep("""                elif position is None and tr.snapshot(bars.iloc[: idx + 1], _xd).ok:""",
    """                elif position is None and (
                        (b3.get("exo_confirm") == "GAP"
                         and (lambda _s: _s * _b3_hist[idx] > 0 and _s * (_b3_hist[idx] - _b3_hist[idx - 1]) > 0)(
                             1.0 if _xd == Direction.UP_RED else -1.0))
                        or (b3.get("exo_confirm") != "GAP" and tr.snapshot(bars.iloc[: idx + 1], _xd).ok)):""")

# HVP: 완성봉 래더 (PLOCK 훅 앞)
rep("""                    # ── 연구 훅 PLOCK: P3-RUNNER 부분 보호 (1회) ──""", """                    # ── 연구 훅 HVDAY: HV 일 첫 수익 보호 (production 조기익절 값 재사용) ──
                    if (b3 is not None and b3.get("hvp") and _hv_active(recognition_at)
                            and float(position["rec"].peak_net_pct) >= float(config.EARLY_TP_TRIGGER_PCT) - 1e-12
                            and net <= float(config.EARLY_TP_FLOOR_PCT) + 1e-12):
                        position["rec"].hvp_fired = True
                        close_trade(recognition_at, px, "HV_PROTECT", idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
                    # ── 연구 훅 PLOCK: P3-RUNNER 부분 보호 (1회) ──""")

P.write_text(src, encoding="utf-8")
print("patched OK")
