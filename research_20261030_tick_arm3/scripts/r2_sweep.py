"""+3% 틱 ARM 본 검증 재생 — 전략 2종 × 변형 A/B/C × 실제 틱 구간(10/16~10/30).

    python .../r2_sweep.py            # 전부
    python .../r2_sweep.py N1         # 한 전략만

전략:
  CHOPX = CHOP 모드 + MU Assist + XS (= 현행 운영 구성)
  N1    = N1 모드 (MU Assist 없음)
변형:
  A 현행 / B TICK_ARM_3_SOFT / C TICK_ARM_3_MEDIUM
공통: 실제 ETF·하이닉스 1초 틱(WK_TICKPX=1). 그 밖의 설정은 세 변형이 완전히 같다.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
RUN = ROOT / "research_20261030_tick_arm3" / "scripts" / "r1_run.py"

DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]

STRATS = {
    "N1":    ["--env", "WK_MODE=N1", "--env", "WK_MUA=0", "--env", "WK_XS=0"],
    "CHOPX": ["--env", "WK_MODE=CHOP", "--env", "WK_MUA=1", "--env", "WK_XS=1"],
}
ARMS = {"A": "NONE", "B": "SOFT", "C": "MEDIUM"}


def main():
    want = [s for s in sys.argv[1:] if s in STRATS] or list(STRATS)
    for st in want:
        for v, arm in ARMS.items():
            tag = f"{st}_{v}"
            cmd = [sys.executable, str(RUN), "--harness", "ARM3", "--var", "ARM3",
                   "--tag", tag, *STRATS[st], "--env", f"WK_ARM={arm}",
                   "--env", "WK_TICKPX=1", "-j", "3", *DAYS]
            print(f"\n######## {tag} ########", flush=True)
            r = subprocess.run(cmd, cwd=str(ROOT))
            if r.returncode != 0:
                print(f"!! {tag} 실행기 실패 rc={r.returncode}", flush=True)


if __name__ == "__main__":
    main()
