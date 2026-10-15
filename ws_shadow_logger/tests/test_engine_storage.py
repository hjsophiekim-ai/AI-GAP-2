import csv
import json
from collections import deque
from datetime import datetime, timedelta

from ws_shadow_logger import aggregator, runner, stub
from ws_shadow_logger.aggregator import SNAPSHOT_COLUMNS
from ws_shadow_logger.engine import Engine
from ws_shadow_logger.storage import Store

DAY = "2026-10-15"


def test_replay_writes_raw_snap_logs(tmp_path):
    frames = stub.session(datetime(2026, 10, 15, 10, 0, 0), seconds=30)
    src = tmp_path / "frames.txt"; stub.write(src, frames)
    eng = runner.replay(str(src), root=tmp_path / "out")
    assert eng.counts["error"] == 0 and eng.counts["pingpong"] == 1 and eng.counts["system"] == 6
    raw = tmp_path / "out" / "raw" / DAY / "frames_10.txt"          # 장중 = 무압축 즉시 append
    assert sum(1 for _ in open(raw, encoding="utf-8")) == len(frames)
    assert not list((tmp_path / "out" / "raw" / DAY).glob("*.gz")), "장중 gzip 금지"
    for sym in ("000660", "0193T0", "0197X0"):
        with open(tmp_path / "out" / "snapshots" / DAY / f"{sym}.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert list(rows[0].keys()) == SNAPSHOT_COLUMNS
        assert 29 <= len(rows) <= 32
        ts = [r["ts"] for r in rows]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
        assert rows[-1]["imb_5"] != "" and rows[-1]["cttr"] != ""
    with open(tmp_path / "out" / "logs" / f"{DAY}.jsonl", encoding="utf-8") as fh:
        meta = [json.loads(line) for line in fh]
    assert sum(m["event"] == "system" for m in meta) == 6
    counters = json.loads((tmp_path / "out" / "state" / f"counters_{DAY}.json").read_text())
    assert counters["raw"] == len(frames)


def test_restart_appends_without_duplicate_snapshots(tmp_path):
    """재시작(새 Store/Engine) 후 같은 날 파일에 이어 쓰고, 이미 쓴 초는 다시 쓰지 않는다."""
    t0 = datetime(2026, 10, 15, 10, 0, 0)
    frames = stub.session(t0, seconds=20)
    first = [f for f in frames if f[0] < t0 + timedelta(seconds=10, milliseconds=500)]
    second = [f for f in frames if f[0] >= t0 + timedelta(seconds=10)]       # 0.5초 겹치게
    for chunk in (first, second):
        st = Store(tmp_path); eng = Engine(st, runner.symbols())
        for t, f in chunk:
            eng.on_frame(f, t)
        eng.advance(chunk[-1][0] + timedelta(seconds=1)); st.close()
    with open(tmp_path / "snapshots" / DAY / "0193T0.csv", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert sum(1 for x in lines if x.startswith("ts,")) == 1, "헤더는 한 번"
    ts = [x.split(",", 1)[0] for x in lines[1:]]
    assert ts == sorted(ts) and len(set(ts)) == len(ts), "중복 초 없음"
    counters = json.loads((tmp_path / "state" / f"counters_{DAY}.json").read_text())
    assert counters["raw"] == len(first) + len(second), "raw 누계가 재시작 뒤 이어진다"


def test_memory_stays_bounded_over_a_long_stream(tmp_path):
    """40분 분량 스트림 — 종목별 deque 는 상한/60초 안, 프레임·행은 메모리에 남지 않는다."""
    t0 = datetime(2026, 10, 15, 10, 0, 0)
    st = Store(tmp_path); eng = Engine(st, runner.symbols())
    for k in range(0, 40 * 60, 30):
        for t, f in stub.session(t0 + timedelta(seconds=k), seconds=30, seed=k):
            eng.on_frame(f, t)
        for s in eng.states.values():
            assert len(s.trades) <= aggregator.MAX_TRADES and len(s.hist) <= aggregator.MAX_HIST
            if s.trades:
                assert (s.trades[-1][0] - s.trades[0][0]).total_seconds() <= 60
    st.close()
    for s in eng.states.values():
        assert isinstance(s.trades, deque) and s.trades.maxlen == aggregator.MAX_TRADES
    assert not [v for v in vars(st).values() if isinstance(v, list)], "Store 가 리스트를 쌓지 않는다"
