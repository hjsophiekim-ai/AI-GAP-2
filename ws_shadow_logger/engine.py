"""프레임 → 상태 → 1초 스냅샷 → 저장. 네트워크와 무관한 핵심 루프라 live / stub replay 가 같은 코드를 쓴다.

프레임은 받는 즉시 raw 파일에 append 하고 버린다 — 메모리에 누적하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .aggregator import SymbolState
from .config import GAP_EVENT_SEC
from .parser import FrameError, parse_frame
from .schema import KIND
from .storage import Store


class Engine:
    def __init__(self, store: Store, symbols):
        self.store = store
        self.states = {s: SymbolState(s) for s in symbols}
        self.next_tick = None
        self.counts = {"data": 0, "system": 0, "pingpong": 0, "error": 0, "encrypted": 0, "unknown_tr": 0}
        self.last_message_at: datetime | None = None
        self.last_gap_s: float = 0.0

    def on_frame(self, raw: str, recv: datetime) -> dict:
        """반환: 파싱 결과 (pingpong 이면 호출부가 그대로 응답)."""
        if self.last_message_at is not None:
            gap = (recv - self.last_message_at).total_seconds()
            self.last_gap_s = gap
            if gap > GAP_EVENT_SEC:
                c = self.store.counters
                c["gap_events"] = c.get("gap_events", 0) + 1
                c["max_gap_s"] = max(float(c.get("max_gap_s", 0.0)), round(gap, 3))
                self.store.log(recv, "gap", gap_seconds=round(gap, 3), since=self.last_message_at.isoformat())
        self.last_message_at = recv
        self.store.raw(recv, raw)
        self.advance(recv)
        try:
            msg = parse_frame(raw)
        except FrameError as e:
            self.counts["error"] += 1
            self.store.log(recv, "parse_error", error=str(e), head=raw[:80])
            return {"type": "error"}
        self.counts[msg["type"]] = self.counts.get(msg["type"], 0) + 1
        if msg["type"] == "data":
            kind = KIND[msg["tr_id"]]
            for rec in msg["records"]:
                st = self.states.get(rec.get("MKSC_SHRN_ISCD"))
                if st is None:
                    continue
                (st.on_trade if kind == "trade" else st.on_book)(rec, recv)
        elif msg["type"] == "system":
            self.store.log(recv, "system", **{k: msg.get(k) for k in ("tr_id", "tr_key", "rt_cd", "msg_cd", "msg1")})
        return msg

    def advance(self, now: datetime):
        """초 경계마다 스냅샷 기록. 프레임이 없는 초도 시계 tick 으로 채운다."""
        if self.next_tick is None:
            self.next_tick = now.replace(microsecond=0) + timedelta(seconds=1)
        while self.next_tick <= now:
            for st in self.states.values():
                self.store.snapshot(st.snapshot(self.next_tick), self.next_tick)
            self.next_tick += timedelta(seconds=1)
