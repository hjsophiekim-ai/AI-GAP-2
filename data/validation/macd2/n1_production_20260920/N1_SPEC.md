# N1 연구사양 → production 이식 사양표 (2026-09-20)

근거는 전부 **연구엔진 코드에서 기계추출**했다(추측 없음). 추출 스크립트는
`scratchpad/ax/n1_spec.py`, 원시 출력은 이 폴더의 `n1_spec_extract.txt`.

연구엔진 N1 정의 (`axlib.SPEC["N1"]`):

```python
X = x2lite_params()                       # X2-lite ExitParams
NB = {"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 4.0}   # d_variant
N1 = (cfg=NB,
      po={"trail_stop_pct": 1.5, "aft_tp_pct": 4.0},               # ExitParams override
      q3=True,                                                     # QUALITY_SCORE_THRESHOLD=3
      h50=True)                                                    # small whipsaw HOLD
# run_chain 기본값: morning_only_bypass=True, watch_seed_fix=True,
#                   relax_mode=None, teg_soft=False, runner=None,
#                   adaptive_default=None, regime_release=None, ax=None
```

---

## 1. 진입 (Entry) — N1 = H50 과 **quality 임계값만** 다르다

| 단계 | 연구엔진 N1 | production X2-lite / H50 | N1 이식 |
|---|---|---|---|
| 플래그 | `signal_engine.calculate_macd` + `evaluate_macd_crossover`, 하이닉스 3분봉 | 동일 | **재사용, 무변경** |
| T+3 확정 | 플래그봉 +1 봉에서 판정(`idx == p_idx + 1`) | 동일 (`_judge_tw2_3slot_flag` → `_resolve_tw2_3slot_candidate`) | **재사용, 무변경** |
| TW2 base | `twf.evaluate_time_window_entry(..., morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)` | 동일 (worker 3319행) | **재사용, 무변경** |
| 오후 우회 | `morning_only_bypass=True` — 오후 윈도우가 `TW_REJECT_TIME_WINDOW` 로만 막혔고 14:57 전이면 통과 | **production 에 동일 로직 존재** (`window_blocked_by_morning_only`, worker 3325행) | **재사용, 무변경** |
| TW2 extra veto | `twf.evaluate_tw2_extra_vetoes` | 동일 | **재사용, 무변경** |
| 슬롯 | `tw3.resolve_slot` (하루 3슬롯, 오전 3번째부터 quality, 오후는 TEG, 같은방향 오후 재진입 금지) | 동일 | **재사용, 무변경** |
| **quality gate** | `tw3.evaluate_trend_quality`, **임계값 3** (`q3=True` 가 `config.QUALITY_SCORE_THRESHOLD=3` 으로 전역 변경) | **임계값 4** (`config.QUALITY_SCORE_THRESHOLD`) | **N1 전용 3** — `quality_score_threshold(mode)` 신설 |
| TEG gate | `teg_gate.evaluate_teg` (오후 슬롯) | 동일 | **재사용, 무변경** |
| CHOP→TEG | `requires_chop_teg_gate` | X2-lite 계열 True | **N1 도 True** |
| 신규진입 cutoff | `config.NEW_ENTRY_CUTOFF=14:55`, 플래그는 `SESSION_OPEN ≤ t < cutoff` | 동일 | **재사용, 무변경** |

> **결론: N1 은 진입집합이 H50 과 다르다.** quality 임계값 4→3 하나 때문이며
> 78일에서 H50 157거래 vs N1 158거래. "청산만 다른 overlay" 가 아니다.

## 2. 사이징 (W1a) — 완전 동일

`_w1a_multiplier(entry_chop, first_stop, exposure)` = production `position_sizing`
규칙 그대로. CHOP 0.80 / 첫거래손절후 1.20 / clip [0.25, 1.50] / 일 누적 3.00 cap.
→ **N1 도 `position_sizing.is_active` True 로 편입. 계산식 무변경.**

## 3. 청산 래더 — 여기가 N1 의 본체

### 3-1. 고정 파라미터 (ExitParams)

| 필드 | X2-lite/H50 | **N1** | 출처 |
|---|--:|--:|---|
| stop_loss | −1.30 | −1.30 | 동일 |
| after_tp1_stop | 2.00 | 2.00 | 동일 |
| trail_trigger | 3.50 | 3.50 | 동일 |
| **trail_stop** | 2.80 | **1.50** | N1 변경 |
| **afternoon_tp** | 3.00 | **4.00** | N1 변경 |
| afternoon stop_loss | −1.20 | −1.20 | 동일 |
| afternoon breakeven | 1.50 → 0.20 | 동일 | 동일 |
| afternoon profit_lock | 2.00 → 1.00 | 동일 | 동일 |
| **ETP trigger / floor** | 1.50 / **1.00** | 1.50 / **0.80** | **N1 변경** |

> ⚠ **ETP floor 차이는 실제 차이다.** 연구 `x2lite_params()` 가 `etp_floor_pct` 를
> override 하지 않아 `BASE` 의 `config.EARLY_TP_FLOOR_PCT=0.8` 이 쓰였고,
> production `etp.thresholds()` 는 X2-lite 계열에서 `X2LITE_EARLY_TP_FLOOR_PCT=1.0`
> 을 돌려준다. 연구 앵커(401.0853)를 재현하려면 **N1 은 0.80** 이어야 한다.

### 3-2. adaptive 래더 — 상위추세 여부로 **봉마다** 전환

판정: `tregime.snapshot(bars_3m[:idx+1], 보유방향).ok`

```
LONG(UP_RED) 보유  : close > EMA50  AND  EMA20 > EMA50  AND  EMA50 기울기 > 0
SHORT(DOWN_BLUE)   : close < EMA50  AND  EMA20 < EMA50  AND  EMA50 기울기 < 0
EMA span = config.H50_TREND_EMA_FAST(20) / H50_TREND_EMA_SLOW(50)   ← H50 과 같은 상수
기울기   = EMA50[-1] − EMA50[-2] (완성봉 1봉)
프레임   = 하이닉스 3분봉, 판정시점 이전 완성봉만. 봉수 < 51 이면 insufficient → ok=False
```

| | 추세 ok = True | 추세 ok = False |
|---|--:|--:|
| **TP1** | **3.5** | 3.0 |
| **TP1 매도비중** | **0.0** (= 전량 보유, TP1 은 트레일링 arm 용) | 0.2 |
| **TP2 (effective)** | **8.0** | **4.0** |
| after_tp1_stop | 2.0 | 2.0 |
| trailing_stop | 1.5 | 1.5 |
| 나머지 | 위 3-1 고정값 | 동일 |

- `off_tp2` 규칙: `eff_cfg()` 는 `off_` 접두 키를 **비추세에만**, 나머지를 **추세에만**
  적용한다. 그래서 비추세에서는 `tp1`/`tp1_ratio` 도 X2-lite 기본값(3.0 / 0.2)으로
  **함께 되돌아간다** — TP2 만 바뀌는 게 아니다.
- 판정 주기: **매 완성봉**. 연구엔진은 틱 루프에서도 같은 `idx` 의 판정을 재사용한다
  (`trend_ok_at` 캐시, 키 = (bar idx, 방향)).
- 새 EMA 계산식 없음 — `major_flag_filter._ema` / `_prepare_bars` 재사용.

### 3-3. H50 small whipsaw HOLD — N1 이 **그대로** 쓴다

연구엔진은 production 함수를 직접 호출한다:
`swh.evaluate_hold(bars_slice, held_dir, recognition_at)` /
`swh.evaluate_release(bars, held_dir, started_at, now, trend_break_count)` /
사유 `swh.EXIT_SMALL_WHIPSAW_HOLD`.
상수: `H50_TREND_EMA_FAST/SLOW=20/50`, `H50_RANGE_BARS=20`, `H50_RANGE_MAX_PCT=2.35`,
`H50_TREND_BREAK_BARS=2`, `H50_MAX_HOLD_MIN=60`.

**N1 과 H50 의 차이는 없다 — HOLD/release 로직·상수 전부 동일.** 다른 것은 위
3-1/3-2 의 청산 래더뿐이다. 그래서 N1 은 `small_whipsaw_hold.is_active` 에 편입하고,
`h50_*` state 를 **그대로 공유**한다(별도 namespace 를 만들지 않는다 — HOLD 는
같은 기능이고, 두 모드는 상호배타라 동시에 살아 있을 수 없다).

> `time_window_h50_filter_enabled` 토글은 **H50 모드 선택 플래그**이고,
> N1 모드에서는 그 토글이 꺼져 있어도 HOLD 가 동작한다(N1 사양의 일부).
> 즉 H50 토글 ON/OFF 가 N1 동작을 바꾸지 않는다 — 상호배타이므로 애초에
> 둘이 동시에 ON 될 수 없고, `is_active` 는 "현재 active 모드" 로만 판정한다.

### 3-4. whipsaw-watch

`watch_seed_fix=True` = production `_start_whipsaw_watch` 와 같은 seed
(현재 gap/spread 기록 + 다음 완성봉부터 비교). → **재사용, 무변경.**

### 3-5. 강제청산

`recognition_at.time() >= config.FORCE_LIQUIDATE_AT(15:00)` → 전량.
→ **재사용, 무변경.**

## 4. 청산사유 매핑 (연구엔진 → production ledger)

| 연구엔진 | production | 비고 |
|---|---|---|
| `TIME_WINDOW_TP2_FULL` | `config.EXIT_TW_TP2_FULL` | **N1 진단용으로 effective TP2(8.0/4.0) 를 원장에 별도 기록** |
| `TIME_WINDOW_TP1_PARTIAL` | `config.EXIT_TW_TP1_PARTIAL` | 추세구간에선 비중 0.0 이라 부분매도 주문이 나가지 않고 `tp1_done` 만 arm |
| `TIME_WINDOW_STOP_LOSS` | `config.EXIT_TW_STOP_LOSS` | 동일 |
| `TIME_WINDOW_AFTER_TP1_STOP` | `config.EXIT_TW_AFTER_TP1_STOP` | 동일 |
| `TIME_WINDOW_TRAILING_STOP` | `config.EXIT_TW_TRAILING_STOP` | 동일 |
| `TIME_WINDOW_AFTERNOON_TP` | `config.EXIT_TW_AFTERNOON_TP` | 동일 |
| `TIME_WINDOW_BREAKEVEN_STOP` | `config.EXIT_TW_BREAKEVEN_STOP` | 동일 |
| `TIME_WINDOW_PROFIT_LOCK_STOP` | `config.EXIT_TW_PROFIT_LOCK_STOP` | 동일 |
| `OPPOSITE_SIGNAL` | `config.EXIT_OPPOSITE_SIGNAL` | 동일 |
| `SMALL_WHIPSAW_HOLD_EXIT` | `small_whipsaw_hold.EXIT_SMALL_WHIPSAW_HOLD` | 동일 |
| `WHIPSAW_WATCH_DETERIORATION_EXIT` | `config.WHIPSAW_WATCH_DETERIORATION_EXIT` | 동일 |
| `EARLY_TAKE_PROFIT` | `config.EXIT_EARLY_TAKE_PROFIT` | N1 은 floor 0.80 |
| `FORCED_LIQUIDATION` | `config.EXIT_FORCED_LIQUIDATION` | 동일 |
| `END_OF_DATA` | (없음 — 백테스트 전용) | production 미해당 |

> 사용자가 제시한 `N1_TP2_FULL` / `N1_OFF_TP2` 같은 **새 사유는 만들지 않는다.**
> 이유: 연구엔진이 낸 사유가 `TIME_WINDOW_TP2_FULL` 하나이고, 8.0/4.0 구분은
> **사유가 아니라 그 순간의 effective TP2 값**이다. 새 사유를 만들면 (a) 연구
> 결과와 사유 분포가 어긋나고 (b) 기존 원장/UI 라벨/집계가 깨진다. 대신 원장에
> `n1_effective_tp2_pct` / `n1_regime_ok` / `n1_regime_reason` 3개 컬럼을 **맨 뒤에
> 추가**해 "8%였나 4%였나, 왜 그랬나" 를 진단할 수 있게 한다.

## 5. production 이식 설계

| 대상 | 방법 |
|---|---|
| 모드 | `MODE_N1_3SLOT = "N1_3SLOT"` 신설. `MODES_3SLOT` 에 추가(슬롯/T+3/원장/카운터 공유). **`MODES_X2LITE_FAMILY` 는 건드리지 않는다**(X2-lite/H50 diff 0 보장). |
| 공유 집합 | `MODES_W1A_FAMILY = MODES_X2LITE_FAMILY + (MODE_N1_3SLOT,)` 신설 — `requires_chop_teg_gate` / `position_sizing.is_active` / `small_whipsaw_hold.is_active` 가 이걸 본다. |
| 고정 청산값 | `exit_overrides(N1)` = X2-lite 값 + `trailing_stop 1.5` / `afternoon_tp 4.0`. |
| TP2 기본 | `morning_tp2_pct_override(N1)` = **8.0** (추세 기준값). 비추세 4.0 은 worker 가 override 로 전달. |
| adaptive | 신규 순수 모듈 `n1_adaptive.py` — `regime_ok()` (= tregime.snapshot 동치) + `resolve_ladder()` (tp1/ratio/tp2 3값 반환). |
| TP1 레벨 | `twpm.evaluate_position/evaluate_take_profit_immediate` 에 **`tp1_pct_override` 인자 추가**(기본 None → 기존 동작 불변). 모듈 상수를 런타임에 갈아끼우지 않는다(MU_MACD 와 공유하므로 금지). |
| quality | `tw3.quality_score_threshold(mode)` 신설(N1 → 3, 그 외 `config.QUALITY_SCORE_THRESHOLD`). `twf.evaluate_time_window_entry` 에 `quality_threshold_override` 인자 추가(기본 None → 불변). |
| ETP | `etp.thresholds()` 에 N1 분기 추가 → (1.5, 0.8). |
| HOLD | `swh.is_active` 가 `MODES_W1A_FAMILY` 를 보게 확장. `h50_*` state 공유. |
| C1 | `peak_protection.is_supported_mode` 를 **N1 전용**으로 좁힌다. |
| 주문 | 전부 기존 `order_executor` 경로. broker 직접 호출 0. |

## 6. 기본값

`N1_ENABLED`(kill switch) / `N1_3SLOT_FILTER_DEFAULT = False` / **N1 OFF / C1 OFF**.
과거 state 에 N1 키가 없으면 전부 기본값(OFF). 마이그레이션 자동 ON 없음
(H50 의 `ADOPT_ON_MIGRATION` 같은 인수인계 규칙을 두지 않는다).

## 7. 파리티 한계 (명시)

연구엔진은 **3분봉 루프 백테스트**이고 production 은 **틱 구동 worker** 다.
따라서 "production 코드로 401.0853 을 재현" 의 검증 방식은 C1 과 동일하게:

> 연구엔진이 파라미터·판정식을 **브랜치의 production 모듈에서 읽게** 바꾼 뒤
> 78일을 재생해 앵커가 재현되는지 본다.

이것이 증명하는 것: **브랜치에 올린 N1 정의(모드 분기·adaptive 판정·임계값)가
연구사양과 동치다.** 증명하지 못하는 것: 실시간 틱 루프의 체결시각/체결가.
그쪽은 기존 `test_backtest_worker_clock_parity.py` 계열이 담당한다.
