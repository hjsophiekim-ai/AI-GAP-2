# SHADOW-BASE 80영업일 PARITY — 2026-09-27

`app/trading/macd2/shadow_base.py` 가 연구 BASE(A_BASE)를 실제로 재현하는지
**production 모듈을 직접 돌려** 확인한 결과다. P3 채택 판단의 마지막 게이트였다.

검증창: **80영업일 2026-05-27 ~ 2026-09-22**. BASE = N1 + C1 + SMART + AR1.

---

## 1. 방법

엔진은 판정에 쓰지 않았다. 엔진에서 가져온 것은 **자료 세 가지**뿐이다.

| 입력 | 내용 |
|---|---|
| 3분봉 + 1분 호가 | `ctx81` 덤프 (`s1b_dump.py`) |
| 확정 플래그 스트림 | 573건 DECISION (`s1_flags.py`, A_BASE 앵커 일치 확인) |
| 대조 기준 | A_BASE 171거래 |

그 입력을 **작업트리의** `shadow_base` 에 그대로 먹이고(`s2_parity.py`),
나온 완료거래열을 거래단위로 비교했다(`s3_compare.py`).

A_BASE 재현 앵커: `w1.pkl` 의 A_BASE 와 거래단위 **동일 True**, 복리 457.9459.

## 2. 결과

| 항목 | 결과 |
|---|---|
| A_BASE 거래수 | 171 |
| SHADOW 거래수 | **171** |
| 진입시각 집합 | **100%** (한쪽에만 있는 거래 0건) |
| direction | **171/171 (100%)** |
| slot | **171/171 (100%)** |
| **tp1_hit** | **171/171 (100%)** |
| **h50_intervened** | **170/171 (99.42%)** |
| exit_time | 170/171 (99.42%) |
| exit_reason (라벨 정규화 후) | 168/171 (98.25%) |
| 거래 signature 전체일치 | 167/171 (**97.66%**) |

### regime state — detector 가 실제로 쓰는 값

| 항목 | A_BASE | SHADOW |
|---|---|---|
| 대상 봉 | 11,783 | 11,783 |
| **TREND/CHOP 일치** | — | **11,783 / 11,783 = 100.0000%** |
| CHOP 봉 수 | 2,960 | **2,960** |
| 9월 CHOP 비율 | 98.3% | **98.3%** |

## 3. 잔여 불일치 4건 — 전부 원인 규명

| 진입 | A_BASE | SHADOW | 원인 |
|---|---|---|---|
| 2026-05-28 11:36 | 11:48 `OPPOSITE_SIGNAL` | 11:48 `TIME_WINDOW_STOP_LOSS` | **같은 시각.** 연구엔진은 한 봉 안에서 확정 플래그를 먼저 처리하고, production worker 는 청산 체인이 먼저다. 청산 시점은 같고 귀속 사유만 다르다 |
| 2026-08-21 10:21 | 10:33 `OPPOSITE_SIGNAL` | 10:33 `TIME_WINDOW_STOP_LOSS` | 위와 동일 |
| 2026-09-15 14:00 | `h50_held=True` | `h50_intervened=False` | 위와 동일 — 반대 플래그와 완성봉 청산이 **정확히 같은 시각**(14:36)이다. 청산시각·사유는 일치하고 H50 각인만 다르다 |
| 2026-09-03 11:00 | 09-04 09:03 `STOP_LOSS` | 09-04 08:00 `FORCED_LIQUIDATION` | 연구엔진이 포지션을 **翌日로 이월**했다. production 은 15:00 강제청산 규약이라 이월하지 않는다 |

넷 다 **섀도우 결함이 아니라 연구엔진 ↔ production 의 의미론 차이**다.
그리고 넷 중 어느 것도 regime 을 바꾸지 않는다(위 표: 100.0000% 일치).

## 4. 이 검증이 실제로 잡아낸 결함 5건

parity 를 돌리기 전에는 전부 보이지 않았다. 전부 `shadow_base` 의 실제 버그였다.

1. **하방 래더가 기초자산 봉 종가로 수익률을 계산** — `bars_3m` 은 하이닉스
   3분봉인데 진입가는 ETF 다. 모든 거래가 진입 1분 뒤 TP2 로 청산됐다.
2. **진입 게이트가 슬롯만 봄** — quality / TEG / 시간창 / veto 를 건너뛰어
   BASE 가 거절한 후보에 섀도우가 진입했다.
3. **whipsaw-watch 미구현** — 해당 청산 2건이 통째로 빠졌다.
4. **조기익절이 실거래 포지션 플래그에 묶여 있었다** — `early_take_profit.
   is_active` 가 `time_window_position_active`(=실거래)를 요구해서, 실거래가
   flat 인 동안 섀도우 ETP 가 꺼졌다. ETP 청산 6건 누락.
5. **진입봉을 건너뛰지 않아 손절이 한 봉 일찍 발동**(5건), **C1 이 완성봉
   게이트 없이 틱마다 평가**되고 자리도 확정 플래그 앞이라 같은 시각 충돌 시
   `h50_intervened` 가 달라졌다.

각 계약은 `tests/macd2/test_p3_worker.py` 가 회귀로 잠근다.

## 5. 재현

```
lab80/s1_flags.py    A_BASE + 확정 플래그 스트림 추출   → s1_flags.pkl
lab80/s1b_dump.py    ctx 를 순수 자료형으로 덤프        → s1b_ctx.pkl
lab80/s2_parity.py   production shadow_base 80일 실행   → s2_shadow.pkl
lab80/s3_compare.py  거래단위 + regime 전 구간 대조     → s3_out.txt
```

원본 거래열은 `scratchpad/research_20260927c_shadow_parity/outputs/`
(`base_trades.csv` / `shadow_trades.csv`).
