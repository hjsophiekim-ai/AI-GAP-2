# -*- coding: utf-8 -*-
"""slot_mult 훅 — READ-ONLY 연구. production 무수정.

HOWTO_REPRODUCE.md 의 S6 훅을 그대로 재현하고, KRW 레이어에 필요한 진단값을
Trade 에 덧붙인다. slot_mult 미지정이면 완전한 no-op(앵커 재현).

order:
  "pre_clip"  : raw = 규칙배수 x extra -> clip(0.25,1.5) -> 일노출상한   (연구/기존)
  "post_clip" : clip(규칙배수) x extra -> 일노출상한                      (사용자 명시 순서)
"""
from pathlib import Path

p = Path("hengine5.py")
s = p.read_text(encoding="utf-8")

# -- 1) _w1a_multiplier: extra + 진단 기록 --------------------------------
old = '''def _w1a_multiplier(*, entry_chop: bool, first_stop: bool, exposure: float) -> float:
    raw = 1.0
    if entry_chop:
        raw *= float(config.X2LITE_SIZING_CHOP_MULT)
    if first_stop:
        raw *= float(config.X2LITE_SIZING_POST_STOP_MULT)
    clipped = max(float(config.X2LITE_SIZING_MIN_MULT),
                  min(float(config.X2LITE_SIZING_MAX_MULT), raw))
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - exposure
    if room <= 0:
        return 0.0
    return min(clipped, room)
'''
new = '''_LAST_W1A: dict = {}


def _w1a_multiplier(*, entry_chop: bool, first_stop: bool, exposure: float,
                    extra: float = 1.0, order: str = "pre_clip") -> float:
    lo = float(config.X2LITE_SIZING_MIN_MULT)
    hi = float(config.X2LITE_SIZING_MAX_MULT)
    rules = 1.0
    if entry_chop:
        rules *= float(config.X2LITE_SIZING_CHOP_MULT)
    if first_stop:
        rules *= float(config.X2LITE_SIZING_POST_STOP_MULT)
    if order == "post_clip":
        clipped = max(lo, min(hi, rules)) * float(extra)
        raw = rules * float(extra)
    else:
        raw = rules * float(extra)
        clipped = max(lo, min(hi, raw))
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - exposure
    applied = 0.0 if room <= 0 else min(clipped, room)
    _LAST_W1A.clear()
    _LAST_W1A.update(rules=rules, extra=float(extra), raw=raw, clipped=clipped,
                     room=room, applied=applied,
                     capped=bool(room <= 0 or clipped > room))
    return applied
'''
assert s.count(old) == 1
s = s.replace(old, new)

# -- 2) Trade 진단 필드 ---------------------------------------------------
old = '    w1a: float = 1.0\n'
new = ('    w1a: float = 1.0\n'
       '    w1a_rules: float = 1.0      # CHOP/POST_STOP 규칙배수 (extra 전)\n'
       '    w1a_extra: float = 1.0      # slot_mult 훅이 준 배수\n'
       '    w1a_clipped: float = 1.0    # clip 후, 일노출상한 전\n'
       '    w1a_room: float = 3.0       # 이 진입 직전 남은 노출한도\n'
       '    w1a_capped: bool = False    # 일노출상한에 걸렸는가\n')
assert s.count(old) == 1
s = s.replace(old, new)

# -- 3) run_chain signature ----------------------------------------------
old = "              events: Optional[list] = None,\n"
new = ("              slot_mult=None,                 # dict|callable|None\n"
       "              slot_mult_order: str = \"pre_clip\",\n"
       "              events: Optional[list] = None,\n")
assert s.count(old) == 1
s = s.replace(old, new)

# -- 4) 진입 site ---------------------------------------------------------
old = '''                                mult = _w1a_multiplier(entry_chop=entry_chop,
                                                       first_stop=w1a_first_stop,
                                                       exposure=w1a_exposure)
                                w1a_exposure += mult
                                w1a_seq += 1
'''
new = '''                                if slot_mult is None:
                                    _ex = 1.0
                                elif callable(slot_mult):
                                    _ex = float(slot_mult(slot_number, session, current_day))
                                else:
                                    _ex = float(slot_mult.get(slot_number, 1.0))
                                mult = _w1a_multiplier(entry_chop=entry_chop,
                                                       first_stop=w1a_first_stop,
                                                       exposure=w1a_exposure,
                                                       extra=_ex,
                                                       order=slot_mult_order)
                                _diag = dict(_LAST_W1A)
                                w1a_exposure += mult
                                w1a_seq += 1
'''
assert s.count(old) == 1
s = s.replace(old, new)

# -- 5) Trade 생성에 진단 실어보내기 --------------------------------------
old = "                                            relax=_relax_hit, forced=_forced)\n"
new = ("                                            relax=_relax_hit, forced=_forced,\n"
       "                                            w1a_rules=_diag['rules'],\n"
       "                                            w1a_extra=_diag['extra'],\n"
       "                                            w1a_clipped=_diag['clipped'],\n"
       "                                            w1a_room=_diag['room'],\n"
       "                                            w1a_capped=_diag['capped'])\n")
assert s.count(old) == 1
s = s.replace(old, new)

p.write_text(s, encoding="utf-8")
print("SLOT patch OK")
