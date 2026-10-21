"""MU 무수신 watchdog — 국내 연결은 살아 있는데 RBAQMU 체결·호가만 조용히 멈추는 경우 (2026-10-21).

2026-10-21 12:46 KST 실사례: 웹소켓·국내 6건은 정상인데 RBAQMU 체결(HDFSCNT0)과 호가(HDFSASP0)가
같은 순간 멈췄다. 같은 날 KIS REST 분봉(BAQ)도 12:47~15:29 가 비어 있어 원인은 KIS/시장 쪽 송신
중단으로 확정됐다 — 재구독으로 살아나지 않는 유형이다. 그래서 이 모듈의 핵심은 **재구독 시도 +
그 구간을 데이터 품질 기록으로 남기는 것** 이고, 재구독이 실패해도 MU_STALE 을 유지한 채 끝난다.

규칙 (기본값은 config, 선택적 env 로만 조정):
  · 감시 시간: 한국 장중 09:00~15:30 KST. 장외에는 판정하지 않는다.
  · 감시 대상: RBAQMU 하나. DNASMU 는 마지막 수신시각만 따로 기록한다(판정·재구독 없음).
  · stale: 체결과 호가가 **둘 다** stale_sec(180초) 이상 0건. 한쪽만 조용하면 정상(저유동성).
    직전 10분 평균 수신량(분당 체결+호가)을 같이 기록해 강도(STRONG/WEAK)를 붙인다.
  · 재구독: 해당 feed 의 체결·호가 2건만 해제(tr_type 2) 후 재등록(tr_type 1). 국내·DNASMU 는 건드리지 않는다.
    lease 가 잡혀 있는 동안에는 새 시도를 보내지 않는다 — 중복 등록 방지.
  · 재시도: 즉시 -> 60초 후 -> 이후 300초 간격, 최대 max_attempts 회. 다 쓰면 GIVE_UP(MU_STALE 유지).
  · 복구: 재구독 응답(SUBSCRIBE SUCCESS)만으로는 복구가 아니다. 실제 체결 또는 호가가 다시 와야 RECOVERED.

이 모듈은 I/O 를 하지 않는다. poll()/note_*() 가 돌려주는 이벤트 목록을 service 가 로그·전송으로 옮긴다.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta

TRADE_TR = "HDFSCNT0"
QUOTE_TR = "HDFSASP0"

OK = "OK"
STALE = "STALE"
GAVE_UP = "GAVE_UP"


class MuWatchdog:
    def __init__(self, *, feed: str = "RBAQMU", stale_sec: float = 180.0, retry_delays=(0.0, 60.0),
                 retry_every: float = 300.0, max_attempts: int = 6, reply_timeout: float = 30.0,
                 watch_start: time = time(9, 0), watch_end: time = time(15, 30), weak_rate_per_min: float = 1.0):
        self.feed = feed
        self.stale_sec = float(stale_sec)
        self.retry_delays = tuple(float(x) for x in retry_delays)
        self.retry_every = float(retry_every)
        self.max_attempts = int(max_attempts)
        self.reply_timeout = float(reply_timeout)
        self.watch_start, self.watch_end = watch_start, watch_end
        self.weak_rate_per_min = float(weak_rate_per_min)
        self.last_at: dict = {}                 # (tr, feed) -> 마지막 수신 시각 (DNASMU 포함, 기록용)
        self.counts: dict = {}                  # (tr, feed) -> 오늘 누계
        self._minutes = deque(maxlen=12)        # [(minute, n)] watch feed 체결+호가, 직전 10분 수신률용
        self.armed_at: datetime | None = None   # 마지막 (재)접속 = 구독 등록 시각
        self.status = OK
        self.intervals: list[dict] = []         # 오늘 stale 구간 (manifest 용)
        self.attempts = 0
        self.next_attempt_at: datetime | None = None
        self.lease: dict | None = None          # {"since": dt, "pending": {tr...}, "attempt": n}
        self.resubscribe_total = 0
        self._gave_up_logged = False

    # ── 입력 ───────────────────────────────────────────────────────────────
    def in_window(self, now: datetime) -> bool:
        return now.weekday() < 5 and self.watch_start <= now.time() < self.watch_end

    def on_connected(self, now: datetime):
        """새 연결 = 전체 재등록이 이미 끝났다. 진행 중 lease 는 의미가 없어진다(그 연결과 함께 사라졌다)."""
        self.armed_at = now
        self.lease = None

    def note_data(self, tr: str, feed: str, now: datetime, n: int = 1) -> list[dict]:
        k = (tr, feed)
        self.last_at[k] = now
        self.counts[k] = self.counts.get(k, 0) + n
        if feed != self.feed:
            return []
        m = now.replace(second=0, microsecond=0)
        if self._minutes and self._minutes[-1][0] == m:
            self._minutes[-1] = (m, self._minutes[-1][1] + n)
        else:
            self._minutes.append((m, n))
        if self.status in (STALE, GAVE_UP):
            iv = self.intervals[-1]
            iv.update(stale_end=now.isoformat(timespec="seconds"), recovery_success=True,
                      recovered_by=("tick" if tr == TRADE_TR else "quote"),
                      duration_s=round((now - datetime.fromisoformat(iv["stale_start"])).total_seconds(), 1))
            self.status, self.lease, self.attempts, self.next_attempt_at = OK, None, 0, None
            self._gave_up_logged = False
            return [dict(kind="RECOVERED", feed=self.feed, by=iv["recovered_by"], duration_s=iv["duration_s"],
                         resubscribe_count=iv["resubscribe_count"])]
        return []

    def note_reply(self, tr: str, key: str, ok: bool, msg1: str, now: datetime) -> list[dict]:
        """재구독 응답. 해제(UNSUBSCRIBE) 응답은 무시 — 등록 응답만 센다."""
        if self.lease is None or key != self.feed or tr not in self.lease["pending"]:
            return []
        if "UNSUB" in str(msg1 or "").upper():
            return []
        if not ok:
            self.lease = None
            return [dict(kind="RESUBSCRIBE_FAIL", feed=self.feed, tr_id=tr, reason=f"reply:{msg1}",
                         attempt=self.attempts)]
        self.lease["pending"].discard(tr)
        if not self.lease["pending"]:
            att = self.lease["attempt"]
            self.lease = None
            return [dict(kind="RESUBSCRIBE_OK", feed=self.feed, attempt=att,
                         note="subscribe acked - waiting for real tick/quote before RECOVERED")]
        return []

    # ── 판정 ───────────────────────────────────────────────────────────────
    def _silence(self, tr: str, now: datetime) -> float:
        ref = self.last_at.get((tr, self.feed))
        if self.armed_at is not None and (ref is None or ref < self.armed_at):
            ref = self.armed_at        # 등록 전 시간은 침묵으로 세지 않는다
        return float("inf") if ref is None else (now - ref).total_seconds()

    def _prev10_rate(self, since: datetime) -> float:
        lo = since - timedelta(minutes=10)
        return round(sum(n for m, n in self._minutes if lo <= m < since) / 10.0, 2)

    def poll(self, now: datetime) -> list[dict]:
        """이벤트 목록. kind=RESUBSCRIBE_ATTEMPT 는 service 가 실제 해제·재등록을 보낸다."""
        ev: list[dict] = []
        if not self.in_window(now) or self.armed_at is None:
            if self.status in (STALE, GAVE_UP) and not self.in_window(now):
                ev += self.close(now, reason="watch_window_end")
            return ev
        st, sq = self._silence(TRADE_TR, now), self._silence(QUOTE_TR, now)
        if self.status == OK:
            if st >= self.stale_sec and sq >= self.stale_sec:
                last = max([t for t in (self.last_at.get((TRADE_TR, self.feed)),
                                        self.last_at.get((QUOTE_TR, self.feed))) if t is not None],
                           default=self.armed_at)
                rate = self._prev10_rate(last)
                sev = "STRONG" if rate >= self.weak_rate_per_min else "WEAK"
                self.status, self.attempts, self.next_attempt_at = STALE, 0, now + timedelta(seconds=self.retry_delays[0])
                self.intervals.append(dict(feed=self.feed, stale_start=now.isoformat(timespec="seconds"),
                                           last_data_at=last.isoformat(timespec="seconds") if last else None,
                                           stale_end=None, resubscribe_count=0, recovery_success=False,
                                           severity=sev, prev10_rate_per_min=rate))
                ev.append(dict(kind="STALE", feed=self.feed, tick_silence_s=round(st, 1), quote_silence_s=round(sq, 1),
                               prev10_rate_per_min=rate, severity=sev))
            else:
                return ev
            # STALE 로 바뀐 바로 이 poll 에서 1차 재구독을 낸다(즉시).
        # STALE / GAVE_UP
        if self.lease is not None and (now - self.lease["since"]).total_seconds() >= self.reply_timeout:
            ev.append(dict(kind="RESUBSCRIBE_FAIL", feed=self.feed, reason="reply_timeout", attempt=self.lease["attempt"]))
            self.lease = None
        if self.status == STALE and self.lease is None:
            if self.attempts >= self.max_attempts:
                self.status = GAVE_UP
            elif self.next_attempt_at is not None and now >= self.next_attempt_at:
                self.attempts += 1
                self.resubscribe_total += 1
                self.intervals[-1]["resubscribe_count"] = self.attempts
                self.lease = {"since": now, "pending": {TRADE_TR, QUOTE_TR}, "attempt": self.attempts}
                i = self.attempts
                delay = self.retry_delays[i] if i < len(self.retry_delays) else self.retry_every
                self.next_attempt_at = now + timedelta(seconds=delay)
                ev.append(dict(kind="RESUBSCRIBE_ATTEMPT", feed=self.feed, attempt=i, max_attempts=self.max_attempts,
                               trs=[TRADE_TR, QUOTE_TR], next_in_s=delay))
        if self.status == GAVE_UP and not self._gave_up_logged:
            self._gave_up_logged = True
            ev.append(dict(kind="GAVE_UP", feed=self.feed, attempts=self.attempts,
                           note="MU_STALE kept - no more resubscribe today unless data returns"))
        return ev

    def close(self, now: datetime, reason: str = "session_end") -> list[dict]:
        """열린 stale 구간을 닫는다(복구 없이). 장 마감/세션 종료."""
        if self.status not in (STALE, GAVE_UP):
            return []
        iv = self.intervals[-1]
        iv.update(stale_end=now.isoformat(timespec="seconds"), recovery_success=False, closed_by=reason,
                  duration_s=round((now - datetime.fromisoformat(iv["stale_start"])).total_seconds(), 1))
        self.status, self.lease, self.next_attempt_at = OK, None, None
        return [dict(kind="STALE_CLOSED", feed=self.feed, reason=reason, duration_s=iv["duration_s"],
                     resubscribe_count=iv["resubscribe_count"])]

    # ── 보고 ───────────────────────────────────────────────────────────────
    def health(self) -> str:
        return {OK: "OK", STALE: f"STALE(att={self.attempts})", GAVE_UP: "MU_STALE"}[self.status]

    def summary(self) -> dict:
        return {"feed": self.feed, "stale_sec": self.stale_sec, "status": self.status,
                "mu_stale_intervals": self.intervals,
                "resubscribe_count": self.resubscribe_total,
                "recovery_success": all(iv.get("recovery_success") for iv in self.intervals) if self.intervals else None,
                "last_at": {f"{tr}:{feed}": t.isoformat(timespec="seconds") for (tr, feed), t in self.last_at.items()},
                "counts": {f"{tr}:{feed}": n for (tr, feed), n in self.counts.items()}}
