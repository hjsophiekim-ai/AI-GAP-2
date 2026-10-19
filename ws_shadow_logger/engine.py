"""프레임 → 상태 → 1초 스냅샷 → 저장. 네트워크와 무관한 핵심 루프라 live / stub replay 가 같은 코드를 쓴다.

프레임은 받는 즉시 raw 파일에 append 하고 버린다 — 메모리에 누적하지 않는다.
MU 체결(HDFSCNT0) 레코드는 mu.MuRecorder 로, MU 호가(HDFSASP0) 레코드는 mu_quote.MuQuoteRecorder 로 간다.
두 경로는 서로도, 국내 수집과도 격리돼 있다 — 어느 쪽 예외든 여기서 삼키고 **따로** 센다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .aggregator import SymbolState
from .config import GAP_EVENT_SEC, MU_MAX_ERRORS, MU_TR_IDS
from .mu import MuRecorder
from .mu_quote import MuQuoteRecorder
from .parser import FrameError, parse_frame
from .schema import KIND
from .storage import Store


class Engine:
    def __init__(self, store: Store, symbols, mu: MuRecorder | None = None,
                 mu_quote: MuQuoteRecorder | None = None):
        self.store = store
        self.states = {s: SymbolState(s) for s in symbols}
        self.next_tick = None
        self.counts = {"data": 0, "system": 0, "pingpong": 0, "error": 0, "encrypted": 0, "unknown_tr": 0}
        self.mu = mu if mu is not None else MuRecorder(store.root)
        self.mu_errors = 0
        self.mu_disabled = False
        self.mu_quote = mu_quote if mu_quote is not None else MuQuoteRecorder(store.root)
        self.mu_quote_errors = 0
        self.mu_quote_disabled = False
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
            if kind == "us_trade":
                self._mu_call("on_records", msg["records"], recv)
                return msg
            if kind == "us_book":
                self._mu_quote_call("on_records", msg["records"], recv)
                return msg
            for rec in msg["records"]:
                st = self.states.get(rec.get("MKSC_SHRN_ISCD"))
                if st is None:
                    continue
                (st.on_trade if kind == "trade" else st.on_book)(rec, recv)
        elif msg["type"] == "system":
            self.store.log(recv, "system", **{k: msg.get(k) for k in ("tr_id", "tr_key", "rt_cd", "msg_cd", "msg1")})
            if msg.get("tr_id") in MU_TR_IDS and str(msg.get("rt_cd")) not in ("0", "None"):
                self.store.log(recv, "mu_subscribe_failed", tr_id=msg.get("tr_id"), tr_key=msg.get("tr_key"),
                               msg_cd=msg.get("msg_cd"), msg1=msg.get("msg1"))
        return msg

    def _mu_call(self, name: str, *args):
        """MU 체결 경로 격리. 실패는 세고 기록만 한다. 하루 MU_MAX_ERRORS 를 넘으면 그 기록만 끈다."""
        self._guarded_call("mu", name, *args)

    def _mu_quote_call(self, name: str, *args):
        """MU 호가 경로 격리 — 체결과 **따로** 센다. 호가가 죽어도 체결·국내는 계속 간다."""
        self._guarded_call("mu_quote", name, *args)

    def _guarded_call(self, which: str, name: str, *args):
        if getattr(self, f"{which}_disabled"):
            return
        target = getattr(self, which)
        try:
            getattr(target, name)(*args)
        except Exception as e:  # noqa: BLE001 — 디스크 오류 포함, 국내 수집으로 번지지 않게
            n = getattr(self, f"{which}_errors") + 1
            setattr(self, f"{which}_errors", n)
            c = self.store.counters
            c[f"{which}_errors"] = c.get(f"{which}_errors", 0) + 1
            ts = args[-1] if args and isinstance(args[-1], datetime) else None
            try:
                if ts is not None:
                    self.store.log(ts, f"{which}_error", op=name, error=repr(e)[:200], n=n)
            except OSError:
                pass
            if n >= MU_MAX_ERRORS:
                setattr(self, f"{which}_disabled", True)
                try:
                    target.close()
                except Exception:  # noqa: BLE001
                    pass
                if ts is not None:
                    try:
                        self.store.log(ts, f"{which}_disabled", reason="too_many_errors", n=n)
                    except OSError:
                        pass

    def advance(self, now: datetime):
        """초 경계마다 스냅샷 기록. 프레임이 없는 초도 시계 tick 으로 채운다."""
        if self.next_tick is None:
            self.next_tick = now.replace(microsecond=0) + timedelta(seconds=1)
        while self.next_tick <= now:
            for st in self.states.values():
                self.store.snapshot(st.snapshot(self.next_tick), self.next_tick)
            self.next_tick += timedelta(seconds=1)
        self._mu_call("advance", now)

    def flush(self):
        self.store.flush()
        self._mu_call("flush")
        self._mu_quote_call("flush")

    def close(self):
        self._mu_call("close")
        self._mu_quote_call("close")
