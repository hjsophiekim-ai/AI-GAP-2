# MACD2 TODO (미해결 운영 이슈)

## [OPEN] 09:03 예약매수 / 프리마켓 승계가 일일 누적매수한도를 우회한다 (N1/P3)

- 등록: 2026-10-06 (E 전략 parity 작업 중 발견)
- 대상: `worker._execute_scheduled_entry`, `worker._execute_premarket_carry_entry`
- 현상: 두 경로는 `execute_signal(budget=state.budget)` 을 W1a 배수·잔여한도 확인·
  `position_sizing.note_entry` 없이 호출한다. 그래서 일 3,000만원(예산 x 3.0) 한도를
  **소비하지도 준수하지도 않는다**. 같은 날 뒤의 3-SLOT 진입이 여유를 과대평가할 수 있다.
- 현재 상태: E 모드에서만 막았다(`position_sizing.clip_to_daily_cap` /
  `note_external_exposure`, `e_strategy.is_active` 로 한정). **N1/P3 는 기존 동작 그대로**
  (OFF parity 유지를 위해 의도적으로 손대지 않음).
- 할 일: N1/P3 에도 같은 clip/누계 반영을 적용하는 별도 hotfix 브랜치.
  merge 전 OFF parity(예약매수/프리마켓 발생일) 영향 측정 필요.
