"""범용 재생 실행기 (production 무수정 · 격리 워크트리 + 하네스 사본).

    python .../r1_run.py --harness PAR --var ECHOPN 20261028
    python .../r1_run.py --harness B3T --var B      20261028          # N1
    python .../r1_run.py --harness ARM3 --tagenv MODE,MUA,XS,ARM 20261028

env 는 `--env K=V` 를 몇 번이고 줄 수 있고, 출력 파일명 tag 에 들어갈 키는 `--tagenv` 로 고른다.
출력: {SP}/out/{TAG}_{YYYYMMDD}.json  (이미 있으면 건너뛴다)
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

WT = Path(os.environ.get("WT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_arm3"))
SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
RS_TABLE = ROOT / "research_20261004_chop_staged" / "rs_table.json"

HARNESS = {
    "PAR": ROOT / "research_20261029_n1_morning_stop_p70r" / "harness" / "test_zz_par.py",
    "B3T": Path(r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_b3t/tests/macd2/test_zz_b3t.py"),
    "ARM3": ROOT / "research_20261030_tick_arm3" / "harness" / "test_zz_arm3.py",
}


def complete_days():
    days = {}
    for f in (SP / "data").glob("replay_*_1m.csv"):
        parts = f.stem.split("_")
        days.setdefault(parts[1], set()).add(parts[2])
    return sorted(d for d, tags in days.items() if {"hynix", "long", "inverse"} <= tags)


def run(harness, env_extra, tag, d, p, p2):
    out = OUT / f"{tag}_{d}.json"
    if out.exists() and out.stat().st_size > 0:
        return True, "SKIP"
    env = dict(os.environ)
    env.pop("MACD2_SLOW_TP2_ENABLED", None)
    env.update(
        WK_D=d, WK_P=p, WK_P2=p2, WK_FILL="REAL",
        WK_MC_BLOCK_EXIT="0", WK_OPPQ="", WK_Q2="", WK_RUNNER="N1",
        REPLAY_SP=str(SP), RS_TABLE=str(RS_TABLE),
        MACD2_MU_ASSIST_DATA_DIR=str(RT), MACD2_EXEC_STRENGTH_DATA_DIR=str(RT),
        KIS_LOGGER_DATA_DIR=str(RT), ARM3_RT=str(RT),
        WK_OUT=str(out) + ".tmp",
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
    )
    env.update(env_extra)
    name = Path(HARNESS[harness]).name
    bt, log = SP / "bt" / f"{tag}_{d}", SP / "log" / f"{tag}_{d}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    bt.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/macd2/{name}", "-q",
         "-p", "no:cacheprovider", "--basetemp", str(bt)],
        cwd=str(WT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10800,
    )
    log.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
    tmp = Path(str(out) + ".tmp")
    if r.returncode == 0 and tmp.exists():
        tmp.replace(out)
        return True, "OK"
    return False, f"FAIL rc={r.returncode} (로그 {log})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="*")
    ap.add_argument("--harness", default="ARM3")
    ap.add_argument("--var", default=None)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--tag", default=None)
    ap.add_argument("-j", type=int, default=1)
    a = ap.parse_args()

    env_extra = {}
    if a.var:
        env_extra["WK_VAR"] = a.var
    for kv in a.env:
        k, v = kv.split("=", 1)
        env_extra[k] = v
    tag = a.tag or (a.var or "RUN")

    OUT.mkdir(parents=True, exist_ok=True)
    (SP / "bt").mkdir(parents=True, exist_ok=True)
    src = HARNESS[a.harness]
    dst = WT / "tests" / "macd2" / src.name
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    complete = complete_days()
    days = a.days or complete
    jobs = []
    for d in days:
        i = complete.index(d)
        jobs.append((a.harness, env_extra, tag, d, complete[i - 1], complete[i - 2]))
    print(f"[하네스] {src.name}  [tag] {tag}  [env] {env_extra}")
    print(f"[대상] {len(jobs)}일 {days[0]}~{days[-1]} · 병렬 {a.j}", flush=True)
    fails = []
    if a.j <= 1:
        for job in jobs:
            ok, msg = run(*job)
            print(f"  {job[3]} {msg}", flush=True)
            if not ok:
                fails.append(job[3])
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=a.j) as ex:
            futs = {ex.submit(run, *job): job for job in jobs}
            for i, fu in enumerate(as_completed(futs), 1):
                job = futs[fu]
                try:
                    ok, msg = fu.result()
                except Exception as exc:
                    ok, msg = False, f"예외 {exc}"
                print(f"  [{i}/{len(jobs)}] {job[3]} {msg}", flush=True)
                if not ok:
                    fails.append(job[3])
    print(f"\n완료. 실패 {len(fails)}: {fails}")
    print(f"출력: {OUT}")


if __name__ == "__main__":
    main()
