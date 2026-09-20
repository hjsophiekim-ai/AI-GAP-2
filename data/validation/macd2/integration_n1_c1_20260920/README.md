# integration/n1-c1 통합 검증 (2026-09-20)

`main-MACD2 c40f5f5` 기준 임시 integration 브랜치. **main merge 전 검증 전용.**

merge 순서: `feature/n1-production` → `feature/c1-peak-protection`.

## 충돌 해결

2차 merge 에서 9건 충돌. C1 은 이미 `feature/n1-production` 에 cherry-pick 되어
**N1 전용으로 재게이트**된 상태라 같은 영역을 두 계보가 다르게 건드린 결과다.
사용자 지시(N1 = base strategy, C1 = N1 전용 overlay)에 따라 **전부
integration 쪽(= N1 브랜치 판)** 을 채택했다. 실질 차이:

    peak_protection.is_supported_mode:
      MODES_X2LITE_FAMILY  (C1 브랜치 판, 폐기)
      MODES_N1_FAMILY      (채택)

손실 검증: `b87c25a` 가 추가한 20파일 전부 존재, C1 핵심 심볼 누락 0.

## 파리티 (`parity_integration.txt`)

| 항목 | 결과 |
|---|---|
| N1 78일 | 158거래 / **401.0853** · PF 2.5868 · MDD −8.9060 |
| N1+C1 78일 / 30일 | 158거래 / **438.6268** / 66.4800 · PF 2.6582 · MDD 동일 |
| 진입집합 parity | 전용 0 / 진입필드 불일치 0 |
| C1 변경 | **7건 전부 개선(악화 0)** |
| TP2 8% runner | 9건 손상 **+0.0000** |
| 앵커 W70 | X2-lite 176.3247 · H50 204.1759 · N1 396.6787 · N1-Safe 384.4773 (전부 일치) |
| 앵커 78일 | X2-lite 181.09 · H50 206.94 (일치) |

## 전체 회귀

| | 실패 | 통과 |
|---|--:|--:|
| baseline c40f5f5 | 71 | 1400 |
| integration | **71** | 1465 |

UI 그룹 단독: baseline 7 = integration **7** (집합 동일).
전체 스위트의 `test_ui_page` 8건 출입은 기존 `st.button inside st.form` 결함의
테스트 순서 의존성(단독 실행 시 baseline 과 동일한 2건).

## 실거래 경로 dry-run (`live_path_dryrun.txt`)

FLAG(production signal_engine 확정 크로스오버) → T+3 → TW2 base(quality
override 3) → 오후 우회 판정 → extra veto → 슬롯 → quality/TEG → CHOP →
W1a sizing → **order request 직전**까지 전 구간 통과. **주문 0건, broker 호출 0건.**
보유 중 N1 adaptive 래더 + C1 판정도 주문 없이 확인.

## ⚠ MarketData / worker stale — **미해결**

`marketdata_stale_evidence.txt` / `cache_last_bar_per_day.txt`

1. **stale 방어 기구의 회귀테스트가 baseline 에서 이미 red** (4건, 이 merge 와 무관):
   - `test_history_updater_watchdog.py::test_successful_fetch_records_success_time_and_clears_error`
   - `test_history_updater_watchdog.py::test_repeating_the_same_bars_is_not_a_new_success`
   - `test_history_updater_watchdog.py::test_stale_age_resets_once_new_bars_arrive_again`
   - `test_market_data.py::TestQuoteHistoryLockIndependence::test_slow_quote_fetch_does_not_block_history_merge`

2. **근본 원인 지점**: `market_data.py:873`
   `dates = df["datetime"].dt.strftime("%Y%m%d")` (`_trim_to_recent_trading_days`)
   → **pandas 3.0.3 에서 `AttributeError: Can only use .dt accessor with
   datetimelike values`**. 이 함수는 라이브 1분봉 merge 경로
   (`merge_live_1m`) 안에서 호출되고, 여기서 예외가 나면 `self._df_1m = merged`
   에 도달하지 못해 **메모리 1분봉이 전진을 멈춘다**(호가는 계속 갱신됨).
   "1분봉만 멈추고 현재가는 살아 있다" 는 관측 증상과 일치한다.

3. **실측 정황**: `data/cache/replay_*_hynix_1m.csv` 의 일별 마지막 봉
   09-09~09-17 전부 19:59, **09-18 만 14:08** (09-19 KIS 재수신본은 18:40 까지
   존재 → 데이터는 브로커에 있었고 로컬 캡처가 끊긴 것). 단 이 파일은 연구용
   fetch 스크립트가 쓰는 캐시이므로 "14:08 에 한 번 받고 갱신 안 됨" 과도
   구분되지 않는다 — 라이브 worker 의 직접 증거는 아니다.

4. **관측 불가 사유**: 이 작업본의 운영 원장
   (`data/logs/macd2_*_ledger.csv`)은 마지막 기록이 **2026-08-19 mock(FAKE 주문)**
   이라 라이브 인스턴스가 아니다. 그리고 검증 시점이 **2026-09-20(일) 17:22 KST,
   장 마감**이라 현재가/1분봉/bar_ledger 의 전진을 직접 관측할 수 없었다.

**결론: 전략 merge 준비 완료 / 실거래 운영 안정성 미확정.**
