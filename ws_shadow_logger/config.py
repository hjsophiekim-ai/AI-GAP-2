"""WS SHADOW LOGGER 설정 — production(app/*)과 완전히 분리. app 패키지를 import 하지 않는다.

실행 조건은 **두 가지뿐**이다 (2026-10-15 Render 연결). MU 체결(HDFSCNT0)도 같은 조건으로 같이 모은다:
  · KIS_LOGGER_ENABLED=1
  · KST 수집시간 08:00 ~ 15:30
자동매매 ON/OFF, 신규진입 허용, 전략모드(N1/P3/E/CHOP), UI 세션은 읽지도 않는다.

키: 로거 전용 실전 appkey 만 사용한다 — KIS_LOGGER_APP_KEY / KIS_LOGGER_APP_SECRET.
  production appkey 와 같으면 실행을 거부한다. 비교를 위해 production **appkey 이름의 값만**
  읽어 즉시 sha256 지문으로 바꾸고 원문은 보관하지 않는다. production secret 은 읽지 않는다.
  REST 접근토큰은 발급하지 않는다. WebSocket 접속키(oauth2/Approval)만 발급한다.

저장: 모든 영구 데이터는 KIS_LOGGER_DATA_DIR 아래에만 둔다(필수). 소스 디렉토리 / 임시 디렉토리는 거부.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ENV_ENABLED = "KIS_LOGGER_ENABLED"
ENV_APP_KEY = "KIS_LOGGER_APP_KEY"
ENV_APP_SECRET = "KIS_LOGGER_APP_SECRET"
ENV_DATA_DIR = "KIS_LOGGER_DATA_DIR"
ENV_MAX_RSS_MB = "KIS_LOGGER_MAX_RSS_MB"
#: 같은 키인지 **비교만** 한다(지문). secret 계열은 목록에 없다 = 읽지 않는다.
PRODUCTION_KEY_ENVS = ("KIS_APP_KEY", "KIS_REAL_APP_KEY", "KIS_MOCK_APP_KEY")

REST_BASE = "https://openapi.koreainvestment.com:9443"       # Approval 발급 전용
APPROVAL_PATH = "/oauth2/Approval"
WS_URL = "ws://ops.koreainvestment.com:21000"                 # KIS 공식 kis_devlp.yaml 'ops'
MAX_REGISTRATIONS = 40                                        # 공식 예제 kis_auth.KISWebSocket 검사값

# 첫 배포 = 3종목 (하이닉스 통합 + 레버리지/인버스 KRX). 체결/호가 각 1건 = 6건 등록.
SUBSCRIPTIONS = (
    ("H0UNCNT0", "000660"), ("H0UNASP0", "000660"),
    ("H0STCNT0", "0193T0"), ("H0STASP0", "0193T0"),
    ("H0STCNT0", "0197X0"), ("H0STASP0", "0197X0"),
)
# MU (Micron, 나스닥) 실시간 체결 — HDFSCNT0, 같은 로거 세션에 추가 등록 (2026-10-15).
#   RBAQMU = 미국 주간거래(나스닥 BAQ, 공식: "R" + 시장구분 BAQ + 종목). 하이닉스 장중과 겹치는 MU 시세.
#   DNASMU = 나스닥 정규/시간외(공식 예제 "DNASAAPL" 형식). 주간거래 시작 전 아침 구간을 채운다.
# production mu_macd(app/trading/mu_macd/config.py)와 같은 tr_key 지만 키·세션이 다르다(로거 전용 appkey).
# 국내 6건을 먼저 등록하고 MU 를 뒤에 등록한다 — MU 등록이 거부돼도 국내 구독은 이미 끝나 있다.
ENV_MU_ENABLED = "KIS_LOGGER_MU_ENABLED"
MU_SUBSCRIPTIONS = (("HDFSCNT0", "RBAQMU"), ("HDFSCNT0", "DNASMU"))
MU_SYMBOL = "MU"
#: 분봉 확정 — 다음 분 체결이 오거나, 시계가 분 끝 + 이 초를 지나면 확정해 쓴다.
MU_BAR_GRACE_SEC = 5.0
#: MU 경로 오류가 하루 이 횟수를 넘으면 그날 MU 기록만 끈다(국내 수집은 계속).
MU_MAX_ERRORS = 50
#: MU 를 붙인 뒤 연결이 이 초 안에 끊기기를 연속 이 횟수 반복하면 그날 MU 등록을 뺀다.
MU_SHORT_SESSION_SEC = 30.0
MU_MAX_SHORT_SESSIONS = 3

SESSION_START = time(8, 0)
SESSION_END = time(15, 30)
SNAPSHOT_SEC = 1.0
DELTA_WINDOWS = (1, 5, 10)        # 초
#: 실시간 계산이 들고 있는 이력의 상한(초) — aggregator 의 deque 는 이보다 오래된 것을 버린다.
MAX_HISTORY_SEC = 60
HEALTH_EVERY_SEC = 60
#: 메시지 간격이 이보다 길면 gap 이벤트로 기록 (join 의 data_ok 기준 5초와 같다).
GAP_EVENT_SEC = 5.0

#: RSS 한도 기본값 — 컨테이너 메모리 한도(cgroup)의 18%. 한도를 못 읽으면 FALLBACK.
RSS_DEFAULT_FRACTION = 0.18
RSS_FALLBACK_MB = 96
RSS_FLOOR_MB = 64

#: 종료 코드 — 래퍼(scripts/render_start_with_logger.sh)는 이 코드들에서 재시작하지 않는다.
EXIT_OK = 0              # 정상 종료 / 비활성
EXIT_CONFIG = 2          # 키 없음·production 키와 동일
EXIT_DISK = 3            # 데이터 경로 없음·쓰기 불가·영구 디스크 아님
EXIT_RSS = 4             # 메모리 한도 초과
NO_RESTART_CODES = (EXIT_OK, EXIT_CONFIG, EXIT_DISK, EXIT_RSS)


class ConfigError(RuntimeError):
    pass


def enabled() -> bool:
    return (os.environ.get(ENV_ENABLED) or "").strip() == "1"


def mu_enabled() -> bool:
    """기본 ON. KIS_LOGGER_MU_ENABLED=0 이면 MU 등록을 하지 않는다(국내 6건만)."""
    return (os.environ.get(ENV_MU_ENABLED) or "1").strip() != "0"


def all_subscriptions(mu: bool | None = None) -> tuple:
    """실제로 등록하는 목록. 국내가 먼저, MU 가 뒤."""
    mu = mu_enabled() if mu is None else mu
    return SUBSCRIPTIONS + (MU_SUBSCRIPTIONS if mu else ())


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:12]


def _dotenv_values() -> dict:
    """repo .env 를 os.environ 에 주입하지 않고 읽기만 한다(로컬 개발용, Render 에는 .env 가 없다)."""
    p = REPO / ".env"
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return _h(self.app_key)


def _production_key_fingerprints(env: dict) -> set:
    out = set()
    for k in PRODUCTION_KEY_ENVS:
        v = os.environ.get(k) or env.get(k)
        if v:
            out.add(_h(v))          # 원문은 이 줄을 벗어나지 않는다
    return out


def load_credentials() -> Credentials:
    env = _dotenv_values()
    key = os.environ.get(ENV_APP_KEY) or env.get(ENV_APP_KEY) or ""
    sec = os.environ.get(ENV_APP_SECRET) or env.get(ENV_APP_SECRET) or ""
    if not key or not sec:
        raise ConfigError(f"{ENV_APP_KEY}/{ENV_APP_SECRET} 가 없다 — 로거 전용 appkey 를 환경변수로 넣어야 한다")
    if _h(key) in _production_key_fingerprints(env):
        raise ConfigError("로거 appkey 가 production appkey 와 같다 — 실행 거부 (production 세션/토큰 보호)")
    return Credentials(key, sec)


# ── 데이터 경로 ─────────────────────────────────────────────────────────────
def _forbidden_roots() -> tuple:
    roots = {REPO.resolve(), Path(tempfile.gettempdir()).resolve()}
    for t in ("/tmp", "/var/tmp"):
        if Path(t).exists():
            roots.add(Path(t).resolve())
    return tuple(roots)


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _mount_point(p: Path) -> Path:
    cur = p
    while not os.path.ismount(cur) and cur.parent != cur:
        cur = cur.parent
    return cur


@dataclass
class DataDirCheck:
    ok: bool
    path: str
    reason: str
    mount_point: str = ""
    on_render: bool = False
    persistent: bool | None = None


def check_data_dir(raw: str | None = None, *, forbidden=None, on_render: bool | None = None) -> DataDirCheck:
    """존재 / 쓰기 / (Render 면) 영구 디스크 여부를 확인한다. ok=False 면 로거만 종료한다."""
    raw = os.environ.get(ENV_DATA_DIR) if raw is None else raw
    on_render = bool(os.environ.get("RENDER")) if on_render is None else on_render
    if not raw:
        return DataDirCheck(False, "", f"{ENV_DATA_DIR} 미설정", on_render=on_render)
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return DataDirCheck(False, str(p), "절대경로가 아니다", on_render=on_render)
    rp = p.resolve()
    for root in (forbidden if forbidden is not None else _forbidden_roots()):
        if _is_under(rp, Path(root)):
            return DataDirCheck(False, str(rp), f"영구 저장 금지 위치({root}) 아래다", on_render=on_render)
    try:
        rp.mkdir(parents=True, exist_ok=True)
        probe = rp / f".write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return DataDirCheck(False, str(rp), f"쓰기 불가: {e!r}"[:200], on_render=on_render)
    mp = _mount_point(rp)
    persistent = None
    if on_render:
        # Render Persistent Disk 는 별도 마운트로 붙는다. 마운트 지점이 루트(/)면 컨테이너 임시 파일시스템이다.
        persistent = str(mp) != str(Path(rp.anchor))
        if not persistent:
            return DataDirCheck(False, str(rp), "Render 에서 영구 디스크 마운트 아래가 아니다(루트 파일시스템)",
                                str(mp), on_render, persistent)
    return DataDirCheck(True, str(rp), "OK", str(mp), on_render, persistent)


# ── 메모리 한도 ─────────────────────────────────────────────────────────────
def container_memory_limit_mb() -> float | None:
    """cgroup v2 / v1 메모리 한도(MB). 없거나 무제한이면 None."""
    for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = Path(f).read_text().strip()
        except OSError:
            continue
        if v and v != "max":
            n = int(v)
            if 0 < n < (1 << 50):          # v1 의 '무제한'은 아주 큰 수로 나온다
                return n / 1024 / 1024
    return None


def max_rss_mb() -> tuple[float, str]:
    """(한도 MB, 근거). 환경변수가 있으면 그 값, 없으면 컨테이너 한도의 18%(최소 64MB)."""
    raw = (os.environ.get(ENV_MAX_RSS_MB) or "").strip()
    if raw:
        return float(raw), f"env {ENV_MAX_RSS_MB}"
    lim = container_memory_limit_mb()
    if lim:
        return max(RSS_FLOOR_MB, round(lim * RSS_DEFAULT_FRACTION)), f"{RSS_DEFAULT_FRACTION:.0%} of cgroup {lim:.0f}MB"
    return float(RSS_FALLBACK_MB), "fallback (cgroup 한도 없음)"


def current_rss_mb() -> float | None:
    """현재 프로세스 RSS(MB). Linux 는 /proc, 그 외는 psutil 이 있으면 사용."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import psutil  # noqa: PLC0415 — 로컬(Windows) 테스트용 선택 의존성
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:  # noqa: BLE001
        return None
