"""
test_broker_factory_compat.py

create_broker() 인터페이스 호환성 테스트.

검증 항목:
  1. runtime_real_mode=True/False 모두 TypeError 없이 동작
  2. runtime_enable_real_buy / runtime_enable_real_sell 파라미터 통과
  3. 알 수 없는 kwargs 전달해도 TypeError 발생하지 않음
  4. 기존 create_broker(mode="mock") 호출 방식 유지
  5. dry_run 모드 기본 동작 유지
"""
import pytest
from unittest.mock import MagicMock, patch

from app.trading.broker_factory import create_broker


class _MinimalCfg:
    """최소 config 스텁 (dry_run 전용)."""
    mode = "dry_run"
    trading = {"total_budget": 1_000_000}
    safety = {}
    _raw = {}


def _make_dry_cfg():
    return _MinimalCfg()


# ---------------------------------------------------------------------------
# 1. runtime_real_mode 인자 수용 여부
# ---------------------------------------------------------------------------

def test_create_broker_accepts_runtime_real_mode_false():
    """runtime_real_mode=False 전달해도 TypeError 없음."""
    cfg = _make_dry_cfg()
    broker = create_broker(cfg=cfg, mode="dry_run", runtime_real_mode=False)
    assert broker is not None


def test_create_broker_accepts_runtime_real_mode_true_dry_run():
    """dry_run 모드에서 runtime_real_mode=True 전달해도 TypeError 없음."""
    cfg = _make_dry_cfg()
    broker = create_broker(cfg=cfg, mode="dry_run", runtime_real_mode=True)
    assert broker is not None


# ---------------------------------------------------------------------------
# 2. runtime_enable_real_buy / runtime_enable_real_sell 인자 수용
# ---------------------------------------------------------------------------

def test_create_broker_accepts_runtime_enable_flags():
    """runtime_enable_real_buy / sell 전달해도 TypeError 없음 (dry_run)."""
    cfg = _make_dry_cfg()
    broker = create_broker(
        cfg=cfg, mode="dry_run",
        runtime_real_mode=False,
        runtime_enable_real_buy=True,
        runtime_enable_real_sell=True,
    )
    assert broker is not None


# ---------------------------------------------------------------------------
# 3. 알 수 없는 kwargs 전달 → TypeError 없이 무시
# ---------------------------------------------------------------------------

def test_create_broker_ignores_unknown_kwargs():
    """미래 확장 kwargs 전달해도 TypeError 없음."""
    cfg = _make_dry_cfg()
    broker = create_broker(
        cfg=cfg, mode="dry_run",
        runtime_real_mode=False,
        unknown_future_flag=True,
        another_flag="value",
    )
    assert broker is not None


# ---------------------------------------------------------------------------
# 4. 기존 호출 방식 (runtime_real_mode 없음) 유지
# ---------------------------------------------------------------------------

def test_create_broker_legacy_call_no_runtime_flags():
    """runtime flags 없이 기존 방식 호출 시 정상 동작."""
    cfg = _make_dry_cfg()
    broker = create_broker(cfg=cfg, mode="dry_run")
    assert broker is not None


def test_create_broker_mode_only():
    """mode만 넘겨도 동작 (cfg는 내부에서 로드 시도)."""
    with patch("app.config.get_config", return_value=_make_dry_cfg()):
        broker = create_broker(mode="dry_run")
    assert broker is not None


# ---------------------------------------------------------------------------
# 5. mock 모드 — KIS 클라이언트 초기화 실패 시 RuntimeError
# ---------------------------------------------------------------------------

def test_create_broker_mock_no_env_raises_runtime_error():
    """mock 환경변수 없으면 RuntimeError 발생 (TypeError 아님)."""
    cfg = _make_dry_cfg()
    cfg.mode = "mock"
    with patch("app.trading.kis_client.create_kis_client", return_value=None):
        with pytest.raises(RuntimeError):
            create_broker(cfg=cfg, mode="mock", runtime_real_mode=False)


# ---------------------------------------------------------------------------
# 6. dry_run 반환 타입
# ---------------------------------------------------------------------------

def test_create_broker_dry_run_returns_dry_run_broker():
    """dry_run 모드는 DryRunBroker 반환."""
    from app.trading.dry_run_broker import DryRunBroker
    cfg = _make_dry_cfg()
    broker = create_broker(cfg=cfg, mode="dry_run")
    assert isinstance(broker, DryRunBroker)
