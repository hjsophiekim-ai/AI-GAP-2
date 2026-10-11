"""READ-ONLY 수신: 20260923(전체본) + 20260928 하이닉스/레버리지/인버스 1분봉 -> scratchpad/b3tp/cache82 만.
공유 data/cache 에는 쓰지 않는다. 장 마감(15:30) 이후에만 실행할 것 (Render 토큰 무효화 위험)."""
import shutil, sys
from datetime import datetime
from pathlib import Path
REPO = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
now = datetime.now()
# 2026-09-28 15:07 사용자 승인: 장중 수신 + KIS 토큰 재발급 허용
import fetch_and_analyze_macd2_today as fa
from app.trading.kis_client import create_kis_client
out = HERE / "cache82"; out.mkdir(exist_ok=True)
for f in (HERE / "cache80").glob("replay_*.csv"):
    if not (out / f.name).exists():
        shutil.copy(f, out / f.name)
client = create_kis_client("real") or create_kis_client("mock")
if client is None:
    sys.exit("KIS client unavailable")
for D in ("20260923", "20260928"):
    frames = fa.fetch_today(client, D)
    for tag, df in frames.items():
        if df is None or df.empty:
            print(D, tag, "EMPTY"); continue
        o = df.copy()
        o["datetime"] = o["datetime"].dt.tz_localize(None)
        o.to_csv(out / f"replay_{D}_{tag}_1m.csv", index=False)
        print(D, tag, len(o), o["datetime"].iloc[0], "..", o["datetime"].iloc[-1])
