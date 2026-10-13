# MACD2 감시 모드 / 신규진입 허용 분리 (2026-10-13)

브랜치 `feature/macd2-entry-arming` (기준 main-MACD2 3e3401e). **main 미merge.**

## 동작
| 상태 | 시세·플래그·전략상태(N1/P3/E/CHOP)·신호원장 | 신규 매수 | 보유 손절/익절/반대신호 청산 |
|---|---|---|---|
| 자동매매 시작 + 신규진입 OFF (감시) | 계속 | **0건** | 계속 |
| 자동매매 시작 + 신규진입 ON | 계속 | ON 이후 **발생한** 신호만 | 계속 |

- 반대신호에서 ON 이었으면 스위치(매도+매수)였을 경우, OFF 면 **보유분 매도만** 한다
  (원장 `SELL_EXECUTED_ENTRY_DISARMED`).
- 소급 금지: 신호 발생시각 = 플래그 3분봉 마감(봉 시작+3분). `entry_armed_at` 이전이면
  `ENTRY_PRE_ARM_SIGNAL` 로 막고 그 signal_id 는 처리완료 — OFF 중의 pending /
  T+3 candidate / E 돌파대기 / pending retry 가 ON 뒤에 체결되지 않는다.
- OFF 중 차단된 신호도 신호원장에 `block_reason=ENTRY_DISARMED` 로 남는다. 슬롯 소비 없음.
- 09:03 예약매수: 발동 시 OFF 면 그날 예약 소진. 프리마켓 승계: OFF 또는 ON 이전 플래그면 소진.
- UI 수동 전량매수: OFF 면 거부(`ENTRY_DISARMED`).
- 전략모드 전환은 이 스위치를 건드리지 않는다.

## 자동 OFF (2026-10-13 2차, 사용자 요청)
- **날짜 변경**: worker `_apply_day_rollover` 가 매 tick `entry_armed_at` 이 오늘이 아니면 OFF.
  판정 함수도 신호 발생일 ≠ ON 한 날이면 `ENTRY_DISARMED` (rollover tick 전에 와도 전날 ON 으로 사지 않음).
  → 매일 아침은 감시만 하는 상태로 시작한다.
- **[자동매매 중지] / [중지 및 일괄매도]**: OFF.
- **[자동매매 시작]**: 항상 OFF 로 시작 — 사용자가 직접 ON 해야 주문.
- 예외: 장중 프로세스 **자동복구**(`_auto_recover_worker`)는 같은 날 이미 켜 둔 값을 유지한다.

## 상태·재시작
`entry_enabled` / `entry_armed_at` / `entry_changed_at` / `entry_changed_by` 를 state 에 저장·복원.
저장값이 없거나 `armed_at` 없는 ON 은 **OFF** 로 읽는다 → **배포 직후에는 UI 에서 직접 ON 해야 주문**이 나간다.
09:03 예약매수는 운영에서 쓰지 않는다 — 발동 시 OFF 면 소진하는 단순 차단만 둔다.

## 구현
- `app/trading/macd2/entry_arming.py` — 판정/토글 순수함수
- `worker._execute_or_wait` — 모든 신호진입의 주문 직전 chokepoint (`_entry_disarmed_outcome`)
- `worker._execute_premarket_carry_entry` / `_execute_scheduled_entry`, `service.manual_entry`
- `service.set_entry_enabled`, UI 버튼 [신규진입 허용 ON] / [신규진입 허용 OFF (감시만)]
- tests: `tests/macd2/test_entry_arming.py` (26건, `@pytest.mark.entry_arming`).
  기존 테스트는 conftest `_entry_arming_open_by_default` 로 허용 상태에서 그대로 돈다.

## 검증
- 신규 31건 통과. 게이트를 무력화하면 16건, 자동 OFF 로직을 빼면 5건이 실패(테스트가 실제로 본다).
- tests/macd2 전체: 66 failed / 2356 passed — 실패집합 = 베이스라인 66건과 동일, 신규 0.
- ON 상태 REAL replay 주문단위 parity vs main 3e3401e: E 5 / CHOP 4 / P3 3 / N1 3 = **15/15일 일치**.

## 함께 고친 원장 표시 버그 (2026-10-13 08:36 BLUE 사례)
- 재시작 catch-up 이 확인시한(플래그 바로 다음 완성봉)이 지난 T+3 후보를 다시 pending 으로 올려,
  첫 tick 이 한참 뒤 봉으로 확인해 "08:36 BLUE" 처럼 보이는 확인행을 남겼다(주문 없음).
  → **플랫일 때** 확인시한이 지난 후보는 pending 없이 `RESTART_T3_EXPIRED` 행으로 바로 정리.
  TW 필터 자체가 플래그 다음 봉이 아니면 `REJECT_NOT_CONFIRMED` 로만 판정하므로 주문 결과는 동일.
  보유 중이면 기존대로(반대신호 청산/휩쏘 보류 판단 불변).
- T+3 확인행(`:TW_CONFIRM` / `:TW2_3SLOT_CONFIRM`)은 `completed_bar_at`/`signal_bar_at` 에
  **원래 플래그 봉 시각**을 표시하고, 확인 처리 봉은 `time_window_confirm_bar_at` 에 남긴다.
- tests: `tests/macd2/test_restart_stale_t3_confirm.py` 3건 + 기존 4건을 새 계약으로 갱신.
- 회귀: 66 failed / 2359 passed, 실패집합 = 베이스라인, 신규 0.
