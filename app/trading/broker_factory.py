"""
broker_factory - 설정(mode)에 따라 적절한 브로커 인스턴스를 생성합니다.

mode:
  "dry_run" -> DryRunBroker  (실제 주문 없음, 메모리 시뮬레이션)
  "mock"    -> KisMockBroker (KIS 모의투자 계좌)
  "real"    -> KisRealBroker (KIS 실전투자 계좌, 6단계 안전장치 필수)
"""

from app.trading.broker_base import BrokerBase
from app.logger import logger


def create_broker(
    cfg=None,
    mode: str = None,
    confirm_text: str = "",
    runtime_real_mode: bool = False,
    runtime_enable_real_buy: bool = False,
    runtime_enable_real_sell: bool = False,
    **kwargs,
) -> BrokerBase:
    """
    설정에 따라 적합한 브로커를 생성합니다.

    mode 우선순위: 파라미터 mode > cfg.mode > "dry_run"

    dry_run: DryRunBroker (API 없음, 메모리 시뮬레이션)
    mock:    KIS 모의투자 계좌 (KIS_MOCK_* 환경변수 필요)
    real:    KIS 실전투자 계좌 (6단계 안전장치 통과 필수)

    runtime flags (UI 실전모드 버튼에서 전달):
      runtime_real_mode          - 실전모드 활성화 (gate 2~3 우회)
      runtime_enable_real_buy    - 실전매수 허용
      runtime_enable_real_sell   - 실전매도 허용
    **kwargs: 미래 확장 또는 구버전 호환 인자 무시 (민감정보 미로그)
    """
    from app.config import get_config
    if cfg is None:
        cfg = get_config()

    effective_mode = mode or cfg.mode or "dry_run"
    logger.info("create_broker: mode=%s runtime_real_mode=%s", effective_mode, runtime_real_mode)

    if effective_mode == "dry_run":
        from app.trading.dry_run_broker import DryRunBroker
        budget = cfg.trading.get("total_budget", 10_000_000)
        return DryRunBroker(initial_balance=float(budget))

    if effective_mode in ("mock", "real"):
        from app.trading.kis_client import create_kis_client
        kis = create_kis_client(effective_mode)
        if kis is None:
            env_hint = (
                "KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, KIS_MOCK_ACCOUNT_NO"
                if effective_mode == "mock"
                else "KIS_REAL_APP_KEY, KIS_REAL_APP_SECRET, KIS_ACCOUNT_NO"
            )
            raise RuntimeError(
                f"브로커 생성 실패: KIS {effective_mode} 클라이언트 초기화 실패. "
                f"실전모드 설정 또는 KIS 환경변수를 확인하세요. "
                f"(.env 파일에 {env_hint} 설정 필요)"
            )

        if effective_mode == "mock":
            from app.trading.kis_mock_broker import KisMockBroker
            return KisMockBroker(kis)

        from app.trading.kis_real_broker import KisRealBroker
        return KisRealBroker(
            kis,
            cfg=cfg,
            confirm_text=confirm_text,
            runtime_real_mode=runtime_real_mode,
            runtime_enable_real_buy=runtime_enable_real_buy,
            runtime_enable_real_sell=runtime_enable_real_sell,
        )

    raise ValueError(f"Unknown mode: {effective_mode}")
