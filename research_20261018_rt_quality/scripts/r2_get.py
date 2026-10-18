"""R2 에서 export zip 하나를 로컬 스크래치패드로 받는다 (2026-10-18). READ-ONLY."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from r1_list import R2, env

DST = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/rt"

def main():
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-16"
    r2 = R2(env())
    key = f"{r2.prefix}{day[:4]}/{day[5:7]}/{day[8:10]}/ws_shadow_export_{day}.zip"
    os.makedirs(DST, exist_ok=True)
    out = f"{DST}/ws_shadow_export_{day}.zip"
    if os.path.exists(out):
        print("이미 있음", out, os.path.getsize(out), "B"); return
    r = r2._get(f"/{r2.bucket}/{key}")
    if r.status_code != 200:
        print("실패", r.status_code, r.text[:200]); return
    open(out, "wb").write(r.content)
    print(f"받음 {out}  {len(r.content):,}B")

main()
