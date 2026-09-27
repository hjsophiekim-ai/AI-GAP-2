"""SHADOW-BASE SLOW CHOP detector — P3 regime stack 의 판정부 (2026-09-27).

이 모듈이 답하는 질문은 하나다: **지금이 CHOP 인가 TREND 인가.**

판정 입력은 오직 ``shadow_base`` 가 기록한 **가상 BASE 거래**의 최근 완료분이다.
실제 체결거래는 절대 쓰지 않는다 -- 2026-09-25 연구(P1)에서 실행기반 detector 는
9월 CHOP 탐지율 98.3% -> 3.6% 로 붕괴했다. B3 가 CHOP 포지션을 일찍 끊으면 그
결과가 다시 detector 입력이 되어 스스로 CHOP 을 지우는 피드백 루프가 생긴다.
SHADOW 는 "P3 가 전혀 없었다면 BASE 가 무엇을 했을지" 를 독립적으로 추적하므로
그 루프가 구조적으로 불가능하다.

판정식(연구엔진 hengine5 의 shadow 산출과 **동일**):

    최근 완료 SHADOW-BASE 거래 10건에 대해
        H50_rate = mean(h50_held)      >= 0.40
        TP1_rate = mean(tp1_hit)       <= 0.20
    둘 다 참이면 CHOP, 아니면 TREND.

"완료" 의 기준시각은 **exit_time** 이다(진입시각이 아니다). 거래는 exit_time
오름차순으로 정렬해 세며, 판정시각보다 **먼저 끝난** 거래만 센다.

10건 미만이면 ``WARMUP`` 이고, WARMUP 은 실거래에서 **무조건 BASE** 다 --
detector 가 준비되지 않았다는 이유로 CHOP 을 추정하지 않는다. 읽기 실패 /
손상 / 예외도 전부 같은 결론(BASE)으로 수렴한다(:func:`current_regime` 참조).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.utils.data_paths import STATE_DIR

REGIME_TREND = "TREND"
REGIME_CHOP = "CHOP"
REGIME_WARMUP = "WARMUP"

#: 이 모듈이 소유하는 유일한 파일. 실거래 ledger / signal ledger 와 절대 섞지
#: 않는다(별도 파일 + 별도 스키마 + 별도 락).
LEDGER_DIR_PATH: Path = STATE_DIR
LEDGER_PATH: Path = LEDGER_DIR_PATH / config.P3_SHADOW_LEDGER_FILENAME

# ledger.py / state_store.py 와 동일한 관례 — import 시점에 기본 경로를 얼려두고,
# 테스트가 경로를 바꿔치기했는지 판단하는 데 쓴다.
_DEFAULT_LEDGER_PATH: Path = LEDGER_PATH
LIVE_WORKER_MARKER_ENV = "MACD2_LIVE_WORKER_PID"

SCHEMA_VERSION = 1

_FILE_LOCK = threading.RLock()


# ── 완료 shadow 거래 한 건 ────────────────────────────────────────────────
@dataclass
class ShadowTrade:
    """완료된 **가상** BASE 거래. 실제 주문과는 아무 관계가 없다.

    ``shadow_trade_id`` 는 재시작/중복저장을 가로막는 안정키다 -- 같은 진입에서
    나온 기록은 프로세스가 몇 번 죽었다 살아나도 같은 id 를 갖는다(진입시각 +
    방향 + 슬롯으로 결정론적으로 만든다. :func:`make_shadow_trade_id`).
    """

    shadow_trade_id: str
    trading_date: str
    entry_time: str
    exit_time: str
    direction: str
    slot: Optional[int] = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    net_pct: float = 0.0
    h50_intervened: bool = False
    tp1_hit: bool = False
    entry_reason: str = ""
    exit_reason: str = ""
    completed: bool = True
    shadow_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_trade_id": self.shadow_trade_id,
            "trading_date": self.trading_date,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "direction": self.direction,
            "slot": self.slot,
            "entry_price": float(self.entry_price),
            "exit_price": float(self.exit_price),
            "net_pct": float(self.net_pct),
            "h50_intervened": bool(self.h50_intervened),
            "tp1_hit": bool(self.tp1_hit),
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "completed": bool(self.completed),
            "shadow_only": True,
        }

    @staticmethod
    def from_dict(raw: Any) -> Optional["ShadowTrade"]:
        if not isinstance(raw, dict):
            return None
        tid = str(raw.get("shadow_trade_id") or "")
        exit_time = str(raw.get("exit_time") or "")
        if not tid or not exit_time:
            # 안정키나 완료시각이 없는 행은 detector 입력으로 쓸 수 없다.
            return None
        try:
            return ShadowTrade(
                shadow_trade_id=tid,
                trading_date=str(raw.get("trading_date") or ""),
                entry_time=str(raw.get("entry_time") or ""),
                exit_time=exit_time,
                direction=str(raw.get("direction") or ""),
                slot=(int(raw["slot"]) if raw.get("slot") is not None else None),
                entry_price=float(raw.get("entry_price") or 0.0),
                exit_price=float(raw.get("exit_price") or 0.0),
                net_pct=float(raw.get("net_pct") or 0.0),
                h50_intervened=bool(raw.get("h50_intervened")),
                tp1_hit=bool(raw.get("tp1_hit")),
                entry_reason=str(raw.get("entry_reason") or ""),
                exit_reason=str(raw.get("exit_reason") or ""),
                completed=bool(raw.get("completed", True)),
                shadow_only=True,
            )
        except (TypeError, ValueError):
            return None


def make_shadow_trade_id(entry_time: Any, direction: Any, slot: Any) -> str:
    """진입시각 + 방향 + 슬롯으로 만드는 **결정론적** 안정키.

    같은 진입은 재시작을 몇 번 하든 같은 id 를 만든다 -- ledger 가 중복 행을
    거부할 수 있는 근거가 이것이다. uuid 를 쓰지 않는 이유도 같다.
    """
    et = entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time or "")
    d = getattr(direction, "value", None) or str(direction or "")
    s = "-" if slot is None else str(slot)
    return f"SB:{et}:{d}:{s}"


# ── 판정 ──────────────────────────────────────────────────────────────────
@dataclass
class RegimeDecision:
    """detector 한 번의 결과. 로그/UI 가 그대로 쓴다."""

    regime: str = REGIME_WARMUP
    h50_rate: Optional[float] = None
    tp1_rate: Optional[float] = None
    sample: int = 0
    window: int = 0
    reason: str = ""
    trades: list[ShadowTrade] = field(default_factory=list)

    @property
    def is_chop(self) -> bool:
        return self.regime == REGIME_CHOP

    @property
    def is_warmup(self) -> bool:
        return self.regime == REGIME_WARMUP


def _sorted_completed(trades: list[ShadowTrade]) -> list[ShadowTrade]:
    """exit_time 오름차순. 연구엔진과 동일하게 **완료시각** 으로 줄세운다."""
    return sorted((t for t in trades if t.completed and t.exit_time),
                  key=lambda t: t.exit_time)


def evaluate(trades: list[ShadowTrade], *, as_of: Optional[str] = None) -> RegimeDecision:
    """SLOW CHOP 판정. ``as_of`` 보다 **먼저 끝난** 거래만 센다.

    ``as_of`` 는 판정시각의 ISO 문자열이다. worker 는 "마지막 완성봉의 완성시각"
    을 넘긴다 -- 즉 그 시각 이후에 끝난 거래는 아직 관측되지 않은 것으로 본다.
    ``None`` 이면 필터 없이 전부(재시작 복원 시 사용).

    ISO 문자열 비교로 정렬/절단하는 것은 두 값이 **항상 같은 타임존(KST)**
    으로 만들어지기 때문이다(shadow_base 가 KST aware isoformat 만 기록한다).
    """
    window = int(config.P3_DETECTOR_WINDOW)
    done = _sorted_completed(trades)
    if as_of:
        done = [t for t in done if t.exit_time < as_of]
    if len(done) < window:
        return RegimeDecision(
            regime=REGIME_WARMUP, sample=len(done), window=window,
            reason=f"WARMUP:{len(done)}/{window}", trades=done[-window:],
        )
    recent = done[-window:]
    h50_rate = sum(1.0 for t in recent if t.h50_intervened) / float(window)
    tp1_rate = sum(1.0 for t in recent if t.tp1_hit) / float(window)
    chop = (h50_rate >= float(config.P3_H50_RATE_MIN)
            and tp1_rate <= float(config.P3_TP1_RATE_MAX))
    return RegimeDecision(
        regime=REGIME_CHOP if chop else REGIME_TREND,
        h50_rate=h50_rate, tp1_rate=tp1_rate, sample=len(done), window=window,
        reason=("CHOP" if chop else "TREND")
               + f":h50={h50_rate:.2f},tp1={tp1_rate:.2f}",
        trades=recent,
    )


def current_regime(*, as_of: Optional[str] = None) -> RegimeDecision:
    """영속 ledger 를 읽어 지금의 regime 을 답한다. **절대 예외를 던지지 않는다.**

    읽기 실패 / JSON 손상 / 스키마 불일치 / 그 밖의 예외는 전부 WARMUP 으로
    수렴하고, WARMUP 은 호출부에서 BASE 로 취급된다(fail-safe = BASE).
    """
    try:
        return evaluate(load_ledger(), as_of=as_of)
    except Exception as exc:  # noqa: BLE001 -- 어떤 이유로도 거래를 막지 않는다
        return RegimeDecision(
            regime=REGIME_WARMUP, sample=0, window=int(config.P3_DETECTOR_WINDOW),
            reason=f"SHADOW_ERROR_FALLBACK:{type(exc).__name__}",
        )


# ── 영속 ledger ───────────────────────────────────────────────────────────
def _assert_safe_to_write_ledger() -> None:
    """ledger.py / state_store.py 와 **동일한** 사고방지 장치.

    경로가 기본값 그대로인데 이 프로세스가 진짜 live worker 가 아니면 거부한다
    -- replay/임시 스크립트가 실제 shadow ledger 를 오염시키는 것을 막는다.
    """
    if LEDGER_PATH != _DEFAULT_LEDGER_PATH:
        return  # 테스트/스크립트가 이미 tmp 로 돌려놨다 -- 안전
    if os.environ.get(LIVE_WORKER_MARKER_ENV) == str(os.getpid()):
        return
    raise RuntimeError(
        "REFUSING to write to the production MACD2 shadow-base ledger "
        f"({_DEFAULT_LEDGER_PATH}). Redirect chop_regime.LEDGER_PATH to a tmp "
        "directory first (mirror tests/macd2/conftest.py's _isolate_macd2_state), "
        "or set os.environ['" + LIVE_WORKER_MARKER_ENV + "'] = str(os.getpid()) "
        "if this genuinely is the live Worker process."
    )


def ensure_paths() -> None:
    LEDGER_DIR_PATH.mkdir(parents=True, exist_ok=True)


def load_ledger() -> list[ShadowTrade]:
    """저장된 완료 shadow 거래를 exit_time 오름차순으로 돌려준다.

    파일이 없거나 깨졌으면 빈 리스트다(예외 없음) -- 호출부는 그 결과로
    WARMUP -> BASE 가 된다.
    """
    with _FILE_LOCK:
        if not LEDGER_PATH.exists():
            return []
        try:
            raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []
    rows = raw.get("trades")
    if not isinstance(rows, list):
        return []
    out: list[ShadowTrade] = []
    seen: set[str] = set()
    for r in rows:
        t = ShadowTrade.from_dict(r)
        if t is None or t.shadow_trade_id in seen:
            continue
        seen.add(t.shadow_trade_id)
        out.append(t)
    return _sorted_completed(out)


def save_ledger(trades: list[ShadowTrade], *, source: str = "worker") -> None:
    """Atomic write: tmp + ``os.replace``, 스레드 락 하에서.

    state_store.save_state 와 같은 방식이다 -- 중간에 프로세스가 죽어도
    반쪽짜리 파일이 남지 않는다.
    """
    _assert_safe_to_write_ledger()
    keep = max(int(config.P3_SHADOW_LEDGER_KEEP), int(config.P3_DETECTOR_WINDOW))
    rows = _sorted_completed(trades)[-keep:]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(config.KST).isoformat(),
        "source": str(source or "worker"),
        "trades": [t.to_dict() for t in rows],
    }
    with _FILE_LOCK:
        ensure_paths()
        tmp = LEDGER_PATH.with_suffix(
            LEDGER_PATH.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, LEDGER_PATH)


def append_trade(trade: ShadowTrade, *, source: str = "worker") -> bool:
    """완료 shadow 거래 한 건 추가. 같은 ``shadow_trade_id`` 는 무시한다.

    반환값은 "실제로 새로 기록했는가" 다 -- 재시작 직후 같은 거래를 다시
    닫으려는 경로에서 중복저장을 막는 지점이 여기다.
    """
    existing = load_ledger()
    if any(t.shadow_trade_id == trade.shadow_trade_id for t in existing):
        return False
    existing.append(trade)
    save_ledger(existing, source=source)
    return True
