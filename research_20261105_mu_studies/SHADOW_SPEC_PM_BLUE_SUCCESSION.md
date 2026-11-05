# SHADOW 사양 — 장전 HY BLUE 승계 → 09:06 가상 인버스 (PM_BLUE_SUCCESSION)

상태: **사양만 (2026-11-05). 구현·배포 전.** 실제 구현은 장 마감 후 별도 지시로 진행.
원칙: **기록 전용. 주문·슬롯·사이징·청산·원장 기존 컬럼 등 production 주문 동작에 영향 0.**

---

## 1. 판정 규칙 (모두 만족해야 기록 대상)

| # | 조건 | 정의 (기존 production 값만 사용, 새 임계값 없음) |
|---|---|---|
| 1 | 08:40~09:00 HY BLUE 플래그 | 바 원장 `macd2_bar_ledger.csv` 에서 `bar_at` 08:39~08:57 봉 중 `confirmed_cross=True` ∧ `direction=DOWN_BLUE`. 확정 시각 = `bar_at`+3분. 여러 개면 마지막 것 |
| 2 | HY BLUE 확정 시점에 MU 도 BLUE | 로거 `mu/<날짜>/bars_1m.csv` → production `mu_assist.load_mu_1m` → 확정 시각까지 완성된 1분봉의 `_macd_hist` 마지막 값 < 0 |
| 3 | MU BLUE 발생~HY BLUE 확정 사이 반대 전환 없음 | 위 hist 에서 확정 시각 이전 마지막 +→− 전환 시각(= MU BLUE 발생) 이후 확정 시각까지 hist > 0 인 분이 없음. (확정 **이후** 08:59 까지 MU 재전환은 허용 — 11/04 사례) |
| 4 | 장전이라 미진입된 신호만 | 그 날 09:00 전 MACD2 주문 0건 ∧ 해당 HY 플래그 signal_id 가 실행원장 `macd2_execution_ledger.csv` 에 없음 |

주의: 08:48 봉은 2026-10-01 패딩 hotfix 이후 신호 경로에서 탈락할 수 있다(바 원장에는 기록됨). 판정은 바 원장 기준으로 하고, `hy_bar_in_signal_path` 로 따로 표시한다.

## 2. 기록 항목 (하루 최대 1행)

| 컬럼 | 내용 |
|---|---|
| `trading_date` | YYYYMMDD |
| `hy_blue_bar_at`, `hy_blue_confirmed_at` | HY BLUE 플래그봉 / 확정 시각 |
| `hy_gap`, `hy_prev_gap` | 바 원장 `gap`, `prev_gap` (확정봉) |
| `mu_blue_since` | MU BLUE 발생(마지막 +→− 전환) 분 |
| `mu_hist_at_confirm` | 확정 시각 MU 1분 hist |
| `mu_reflip_after_confirm` | 확정 후 08:59 까지 MU RED 재전환 여부 (판정에는 안 씀, 관찰용) |
| `hy_bar_in_signal_path` | 그 봉이 production 신호 경로에 들어갔는지 (08:48 봉 탈락 표시) |
| `entry_0906` | 09:06 가상 인버스 진입가 = 09:06:00 첫 체결가(스냅샷 `0197X0` price) + 1틱(5원) |
| `touch_first` | `+1.5` / `-1.5` / `NONE` (15:19 까지, 같은 초면 `-1.5` 우선) |
| `touch_plus_at`, `touch_minus_at` | +1.5% / −1.5% 각각 최초 도달 시각 |
| `ret_1000`, `ret_close` | 10:00, 15:19 수익률 (%) |
| `mfe`, `mae` | 15:19 까지 최대 유리/불리 (%) |
| `actual_0900_1000_orders` | 같은 날 09:00~10:00 실제 MACD2 주문(심볼·방향) — 충돌 확인용 |
| `data_source`, `evaluated_at` | 사용한 원천(로거 스냅샷/KIS 분봉), 계산 시각 |

참고 청산 판단(연구 결과): 고정 +1.5 익절은 승자 수익의 85~90% 포기 → 평가는 위 항목으로 두 방식(+1.5 익절 vs −1.5 손절+보유) 모두 사후 계산 가능하게 둔다.

## 3. 기록 방식 설계 — 장중 추가 메모리 거의 0

**장중(worker 루프)에는 아무 것도 추가하지 않는다.** 판정에 필요한 값이 이미 장중에 디스크에 남고 있고,
판정이 "그 시각까지의 데이터만" 쓰므로 장 마감 후 사후 계산해도 실시간 판정과 같다(look-ahead 없음).

- **실행 시점**: 하루 1회, 장 마감 후. 로거 `maintenance`(15:30 이후) 또는 별도 1회성 스크립트.
  worker·주문 경로·`app/trading/macd2/worker.py` 무수정.
- **입력 (전부 기존 파일, 필요한 시간대·컬럼만 스트리밍)**
  - HY 플래그: `macd2_bar_ledger.csv` — 그 날 08:30~09:00 행만 (production MACD 값 그대로, 재계산 없음)
  - MU: 로거 `mu/<날짜>/bars_1m.csv` — 08:00~09:00 DNASMU 약 60행
  - 진입가·터치·수익률: 로거 `snapshots/<날짜>/0197X0.csv` 에서 `ts`,`price` 두 컬럼만, 09:06~15:19 구간을 한 줄씩 읽으며
    누적 최대/최소·터치 시각만 유지(전체 적재 없음, 메모리 O(1))
  - 주문 충돌: `macd2_execution_ledger.csv` 그 날 09:00~10:00 행
- **출력**: `macd2_pm_blue_succession_shadow.csv` 에 그 날 1행 append (조건 미충족이면 `qualified=False` 로 1행, 사유 포함).
  R2 일별 export 대상에 포함되도록 로거 data dir 아래에 둘지, MACD2 data dir 에 둘지는 구현 시 결정.
- **멱등**: 같은 날짜가 이미 있으면 덮지 않는다.
- **실패 처리**: 어떤 입력이 없어도 예외를 밖으로 내지 않고 `data_missing=<항목>` 으로 기록.

## 4. 구현 시 검증 (장 마감 후 별도 지시 시)

- 과거 재계산: 10/22 → qualified, 11/04 → qualified, 10/28 → 08:33 이라 제외 가 되는지(연구 스크립트 `scripts/hy_pm_blue.py` 결과와 대조).
- 주문 경로 무변경 확인: worker/주문 관련 파일 diff 0.
- 회귀는 신규 모듈 단위 테스트만(전체 `tests/macd2` 회귀는 별도 지시 시).

## 5. 연구 근거 요약

| | 10/22 | 11/04 | (제외) 10/28 |
|---|---|---|---|
| HY BLUE 봉 | 08:45 | 08:42 | 08:33 |
| 09:06 인버스 MAE | −1.03% | −0.56% | −21.22% |
| +1.5 터치 | 09:21 | 09:13 | — (−1.5 09:08) |
| 장마감 | +10.46% | +11.67% | −21.11% |

표본 2건 → production 진입 금지, shadow 로 표본 축적 후 재판정.
