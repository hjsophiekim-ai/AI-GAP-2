"""R2 의 ws_shadow export zip 을 연구용 스크래치로 내려받고 펼친다 (READ-ONLY).

production 무수정. 받는 것은 로거가 이미 올려둔 그날치 zip 뿐이다.

    python research_20261030_tick_arm3/scripts/d0_fetch.py 2026-10-16 ... 2026-10-30

펼친 결과(= 로거 DATA_DIR 와 같은 모양):
    {SP}/rt/snapshots/<YYYY-MM-DD>/{000660,0193T0,0197X0}.csv   ← 1초 스냅샷(체결강도·ETF 틱)
    {SP}/rt/mu/<YYYY-MM-DD>/bars_1m.csv                          ← MU 1분봉
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research_20261018_rt_quality" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")
from r1_list import R2, env  # noqa: E402

SP = Path(os.environ.get("ARM3_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp"))
ZIPS = SP / "zips"
RT = SP / "rt"

DAYS = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
        "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


def main():
    days = sys.argv[1:] or DAYS
    ZIPS.mkdir(parents=True, exist_ok=True)
    RT.mkdir(parents=True, exist_ok=True)
    r2 = R2(env())
    for day in days:
        out = ZIPS / f"ws_shadow_export_{day}.zip"
        if not out.exists():
            key = f"{r2.prefix}{day[:4]}/{day[5:7]}/{day[8:10]}/ws_shadow_export_{day}.zip"
            r = r2._get(f"/{r2.bucket}/{key}")
            if r.status_code != 200:
                print(f"  {day} 실패 HTTP {r.status_code}")
                continue
            out.write_bytes(r.content)
            print(f"  {day} 받음 {len(r.content):,}B")
        with zipfile.ZipFile(out) as z:
            names = [n for n in z.namelist()
                     if n.startswith(("snapshots/", "mu/")) and not n.endswith("/")]
            new = [n for n in names if not (RT / n).exists()]
            if new:
                z.extractall(RT, members=new)
        print(f"  {day} 펼침 ({len(names)}개)")
    print(f"\nRT = {RT}")


if __name__ == "__main__":
    main()
