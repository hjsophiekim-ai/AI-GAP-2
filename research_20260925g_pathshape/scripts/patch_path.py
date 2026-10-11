"""rescue 에 **경로형태(path-shape)** 조건을 추가한다. rescue['path'] 없으면 기존과 동일."""
import pathlib
p = pathlib.Path("hengine5.py"); s = p.read_text(encoding="utf-8"); ok=[]

a = '''    def _gx_rescue_ok(pos, i, rs, tick):
        """+1% 최초도달 틱 판정. 봉 조건은 **마지막 완성봉(i-1)**, ETF 는 1분 호가."""
        F = rs.get("feat")
        j = i - 1
        if F is None or j < 1 or j >= len(F["dgap"]):
            return False, ""
'''
b = '''    def _gx_rescue_ok(pos, i, rs, tick):
        """+1% 최초도달 틱 판정. 봉 조건은 **마지막 완성봉(i-1)**, ETF 는 1분 호가."""
        F = rs.get("feat")
        j = i - 1
        if F is None or j < 1 or j >= len(F["dgap"]):
            return False, ""
        _pth = rs.get("path")
        if _pth is not None:
            up_ = (pos["rec"].direction == "UP_RED")
            sg_ = 1.0 if up_ else -1.0
            _mm = (tick - pos["entry_time"]).total_seconds() / 60.0
            _tags = []
            if _pth.get("max_min") is not None:
                if _mm > float(_pth["max_min"]):
                    return False, ""
                _tags.append("fast%.0f" % _mm)
            if _pth.get("ema20z") is not None:
                _v = F.get("ema20z_up")
                if _v is None or j >= len(_v):
                    return False, ""
                _z = sg_ * float(_v[j])
                if not (_z >= float(_pth["ema20z"])):
                    return False, ""
                _tags.append("ez%.1f" % _z)
            return True, ",".join(_tags)
'''
assert s.count(a)==1; s=s.replace(a,b); ok.append("path rule")
p.write_text(s, encoding="utf-8"); print("적용:", ok)
