# 재현 방법 — ① 10/28 09:51 진입 parity  ② +3% 틱 ARM 수익보호

## 전제

- **production 코드 무수정, READ-ONLY.** 재생은 전부 격리 워크트리 + 하네스 **사본**으로 돌린다.
- 워크트리: `C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_arm3` = **현행 HEAD `368e4bf`**
  (기존 연구의 `wt_b3t` 는 `a8f80e3` 라 `exec_strength` / `mu_catchup` 이 아예 없다 — 쓰면 안 된다).
- 하네스: `harness/test_zz_arm3.py` — `scripts/h0_make.py` 가 10/29 parity 하네스
  (`research_20261029_n1_morning_stop_p70r/harness/test_zz_par.py`)에 **덧붙이기만** 해서 만든다.
  원본 patch 대상이 1개가 아니면 생성 자체가 실패한다(조용한 어긋남 방지).
- KIS 는 조회 전용(재생 1분봉 보충)만 부른다. R2 는 로거가 올려둔 export zip 읽기만 한다.

## 데이터

```bash
# 1) 1초 스냅샷(ETF 실틱 + 체결강도) + MU 분봉 — R2 export zip 12일치
python research_20261030_tick_arm3/scripts/d0_fetch.py
#    -> {ARM3_SP}/rt/snapshots/<날짜>/{000660,0193T0,0197X0}.csv , {ARM3_SP}/rt/mu/<날짜>/bars_1m.csv

# 2) 재생 1분봉 10/30 보충 (10/29 까지는 기존 연구 데이터 재사용)
python research_20261020_h50_hold/scripts/fetch_replay_bars.py 20261030 20261030
cp research_20261020_h50_hold/data/replay_20261030_*.csv {REPLAY_SP}/data/
```

기본 경로(환경변수로 바꿀 수 있다):

| 변수 | 기본값 | 내용 |
|---|---|---|
| `ARM3_SP` | `.../Temp/claude/arm3_sp` | zip + 펼친 1초 스냅샷 |
| `ARM3_RT` | `{ARM3_SP}/rt` | 하네스가 읽는 로거 DATA_DIR |
| `ARM3_OUT`| `{ARM3_SP}/out` | 재생 산출 json |
| `REPLAY_SP`| `.../Temp/claude/b3t_sp` | 재생 1분봉 + 전일 shadow 원장 seed |
| `WT` | `.../Temp/claude/wt_arm3` | 격리 워크트리 (HEAD 368e4bf) |

## 하네스 생성 + 기준선 동등성 확인

```bash
python research_20261030_tick_arm3/scripts/h0_make.py

# 새 하네스가 기존 기준선(ECHOPN)과 같은지 — 주문 4건이 바이트 단위로 같아야 한다
python research_20261030_tick_arm3/scripts/r1_run.py --harness ARM3 --var ARM3 \
  --tag CHK_CHOP_BASE --env WK_MODE=CHOP --env WK_MUA=0 --env WK_XS=0 \
  --env WK_ARM=NONE --env WK_TICKPX=0 20261028
```

## ① 10/28 09:51 진입 parity

```bash
for cfg in "CHOP_MUA_XS:CHOP:1:1:0" "CHOP_MUA_NOXS:CHOP:1:0:0" "CHOP_MUA_XS_NOCU:CHOP:1:1:1" \
           "N1:N1:0:0:0" "N1_MUA_XS:N1:1:1:0" "P3:P3:0:0:0" "E:E:0:0:0"; do
  IFS=: read tag mode mua xs cu <<< "$cfg"
  python research_20261030_tick_arm3/scripts/r1_run.py --harness ARM3 --var ARM3 --tag "P0_$tag" \
    --env WK_MODE=$mode --env WK_MUA=$mua --env WK_XS=$xs --env WK_CUKILL=$cu \
    --env WK_ARM=NONE --env WK_TICKPX=0 20261028
done
python research_20261030_tick_arm3/scripts/p1_parity.py 20261028 --flag 092100 --flag 093900
```

`WK_CUKILL=1` 은 `config.MU_CATCHUP_KILL` 비상 스위치를 켠 것과 같다(= MU CATCH-UP 판정 자체를 안 함).
XS 단독 효과를 보려면 `CHOP_MUA_XS` vs `CHOP_MUA_NOXS` 를, CATCH-UP 단독 효과를 보려면
`CHOP_MUA_XS` vs `CHOP_MUA_XS_NOCU` 를 본다.

## ② +3% 틱 ARM

```bash
python research_20261030_tick_arm3/scripts/r2_sweep.py          # N1 · CHOPX × A/B/C × 11일
python research_20261030_tick_arm3/scripts/a2_metrics.py \
    A=N1_A B=N1_B C=N1_C --days 20261016 ... 20261030 --label "N1" \
    --csv research_20261030_tick_arm3/out/trades_N1.csv
python research_20261030_tick_arm3/scripts/a2_metrics.py \
    A=CHOPX_A B=CHOPX_B C=CHOPX_C --days ... --label "CHOP+MU Assist+XS" \
    --csv research_20261030_tick_arm3/out/trades_CHOPX.csv
```

### 변형 정의 (사용자 사전등록, 최적화하지 않는다)

| | ARM 조건 | 보호선 |
|---|---|---|
| **B** `TICK_ARM_3_SOFT` | 틱 순수익 +3.0% 최초 도달 | +1.5% → (+3.5%) +2.0% → (+4.0%) +2.5% |
| **C** `TICK_ARM_3_MEDIUM` | 같음 | +2.0% → (+3.5%) +2.5% → (+4.0%) +3.0% |

- 최고수익 = **실제 1초 틱**(`price`)으로 잰 순수익. 보호선 **이탈 판정 = 최우선 매수호가**(`best_bid`).
- 보호선은 한 번 올라가면 내려가지 않는다. **+5% 이상에서 더 올리지 않는다** — 사다리 최고단이
  `peak ≥ 4.0` 하나뿐이라 그 위는 기존 TP/C1 러너 규칙이 그대로 지배한다.
- 기존 TP/SL/C1/B3/H50/반대신호/시간청산/15:00 강제청산은 **한 줄도 바꾸지 않았다**.
  오버레이는 `worker._advance_held_position_risk_management` 를 감싸고, **기존 위험관리가
  그 tick 에 아무것도 하지 않았을 때만** 판정한다(= "먼저 발생한 것 적용").

## 함정

1. **`ex` 원장의 `timestamp` 는 벽시계다.** 재생 시각은 `orders[].t` 에만 있다. `ex` 는 전일(P)
   선재생분까지 들고 있으므로 BUY 의 `signal_id` 날짜로 그 날만 걸러야 한다(`a2_metrics.episodes`).
2. **`arm3` / `gates` 로그도 전일분이 섞인다.** 집계 전에 `t[:10]` 으로 그 날만 거른다.
3. **MU Assist 를 켜면 재생이 3배 느려진다**(2분 → 7분/일). T2/P70R SHADOW 가 HEAD 기본 ON 이라
   매 tick 기록을 시도한다. 산출에는 영향이 없다(전부 tmp 로 간다).
4. **기존 `wt_b3t` 워크트리(a8f80e3)로는 이 연구를 돌릴 수 없다** — `exec_strength` import 에서 죽는다.
5. **1초 스냅샷은 10/15 가 불완전하다**(수집률 19%). 10/16 재생의 전일(P) seed 로만 쓰이고,
   스냅샷이 90초 이상 낡으면 하네스가 1분봉 보간으로 자동 폴백한다.
6. **루프 격자는 20초인데 ARM3 의 최고가·이탈 탐지는 1초다.** 보호선 판정만 1초 해상도이고
   집행은 그 다음 20초 격자 tick 이다 — 하락 중이면 체결이 **늦어서 불리한** 쪽이라
   B/C 에 유리하게 기울지 않는다. 기존 TP 래더는 A/B/C 모두 20초 격자 그대로다.

---

## ③ 전략별 전 거래내역 (부록 4장)

```bash
# ① CHOP + MU Assist + XS  (= CHOPX_A, 위 ② 절에서 이미 만든 산출)
# ② N1  + MU Assist + XS
python research_20261030_tick_arm3/scripts/r1_run.py --harness ARM3 --var ARM3 \
  --tag N1X_A --env WK_MODE=N1 --env WK_MUA=1 --env WK_XS=1 \
  --env WK_ARM=NONE --env WK_TICKPX=1 -j 3 \
  20261016 20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030

python research_20261030_tick_arm3/scripts/a5_list.py --tag CHOPX_A --label "① CHOP + MU Assist + XS"
python research_20261030_tick_arm3/scripts/a5_list.py --tag N1X_A  --label "② N1 + MU Assist + XS"
python research_20261030_tick_arm3/scripts/a5_list.py --tag N1_A   --label "참고: N1 (MU Assist 없음)"
```

tag 대응 (`{ARM3_OUT}/{tag}_{YYYYMMDD}.json`):

| tag | 모드 | MU Assist | XS | ARM | 실틱 |
|---|---|---|---|---|---|
| `CHOPX_A` / `_B` / `_C` | CHOP | ON | ON | NONE/SOFT/MEDIUM | ON |
| `N1_A` / `_B` / `_C` | N1 | **OFF** | OFF | NONE/SOFT/MEDIUM | ON |
| `N1X_A` | N1 | **ON** | **ON** | NONE | ON |
| `P0_*` | 10/28 parity 전용 (실틱 OFF) | | | | |

**주의**: 2.1 절의 N1 지표는 `N1_A`(MU Assist 없음)이고, 4.3 절의 ②는 `N1X_A`(MU Assist+XS)다.
서로 다른 구성이므로 숫자를 섞으면 안 된다.

---

## ④ 추세약화 확인형 보호 (W1 / W2) — 2026-10-31 후속

```bash
python research_20261030_tick_arm3/scripts/h0_make.py          # 하네스 재생성 (W1/W2 포함)

# 회귀: 새 하네스가 어제 SOFT 와 같은지 (주문 8건 동일해야 한다)
python .../r1_run.py --harness ARM3 --var ARM3 --tag CHK_SOFT_REG \
  --env WK_MODE=CHOP --env WK_MUA=1 --env WK_XS=1 --env WK_ARM=SOFT --env WK_TICKPX=1 20261029

# 본 검정 — 보호선이 ARM 후보가 되는 날만 (나머지는 W ≡ A 가 코드상 보장)
for v in W1 W2; do
  python .../r1_run.py --harness ARM3 --var ARM3 --tag CHOPX_$v \
    --env WK_MODE=CHOP --env WK_MUA=1 --env WK_XS=1 --env WK_ARM=$v --env WK_TICKPX=1 -j 3 \
    20261021 20261023 20261026 20261028 20261029
  python .../r1_run.py --harness ARM3 --var ARM3 --tag N1_$v \
    --env WK_MODE=N1 --env WK_MUA=0 --env WK_XS=0 --env WK_ARM=$v --env WK_TICKPX=1 -j 3 \
    20261019 20261021 20261023 20261029
done
python .../a4_fill.py --strat CHOPX --variants W1 W2
python .../a4_fill.py --strat N1    --variants W1 W2
python .../a2_metrics.py A=CHOPX_A B=CHOPX_B C=CHOPX_C W1=CHOPX_W1 W2=CHOPX_W2 --days ... --label CHOP
python .../a6_weak.py --strat CHOPX --variants A B W1 W2      # 발동 전수 + 신호별 횟수

# 한 거래의 신호 타임라인 (20초 격자, 오프라인 재구성)
python .../a7_trace.py --day 20261029 --entry 09:59:20 --tag CHOPX_A
python .../a7_trace.py --day 20261028 --entry 09:48:20 --tag CHOPX_A
```

### 약화신호 6종 — 전부 production 계산기 재사용, 미래 데이터 없음

| 신호 | 구현 | 데이터 범위 |
|---|---|---|
| ① MACD gap 축소 | `time_window_filter._gap_series()` 보유방향 부호, 최신 완성봉 < 직전 완성봉 | `signal_engine.resample_completed_3m(now)` |
| ② MU 약화 | `mu_assist.evaluate_pattern()` 앵커 `now−5분` 롤링. 보유 중 CONFIRM 을 본 적 있고 현재 STALL/DIVERGE | `load_mu_1m` 이 끝난 1분봉만 |
| ③ 체결강도 약화 | `exec_strength.evaluate(보유방향, now)` == MISMATCH (기준 100 고정) | 직전 60초 1초 스냅샷 |
| ④ 0.5%p 반납 | `peak − 현재 틱 순수익 >= 0.5` | 1초 스냅샷 (now 이하) |
| ⑤ VWAP 우위상실 | `major_flag_filter._session_vwap()` 대비 3분봉 종가 **또는** 직전 1분봉 종가가 불리 | 완성봉만 |
| ⑥ 반대플래그 미확정 | `_persist_tw2_3slot_decision` 추적 — `TIME_WINDOW_PENDING_CONFIRMATION` 이면 pending | 판정 시점 원장 |

판정 불가(결측/stale/예외)는 전부 **False**(신호 없음). 보호선 사다리는 SOFT 와 **동일**하고
달라진 것은 **활성화 조건 하나뿐**이다(임계값 추가 없음).

### 함정 (추가)

7. **`a4_fill.py` 는 A 산출을 그 tag 이름으로 복사한다.** 복사본은 `arm.filled=true` 가 찍힌다.
   `a6_weak.py` 의 `arm3` 로그는 복사본에도 A 것이 그대로 들어가므로, **발동 전수는 실제 재생한
   날에서만 읽어야 한다**(생략일은 애초에 ARM 후보가 아니라 로그가 비어 있다).
8. **`a6_weak.py` 는 같은 날 여러 거래에 같은 ARM 이벤트를 붙여 보여준다**(종목 기준 매칭).
   정확한 거래별 귀속은 `a7_trace.py` 로 확인한다.

---

## ⑤ 2026-10-31 3연구 (전부 기각)

```bash
python research_20261030_tick_arm3/scripts/h0_make.py     # 하네스 재생성 (V/T/TRBP 포함)

# ① 오후 XS VWAP 근접 override
python .../c1_vwapcand.py --tag CHOPX_A                   # 후보 전수 (재생 불필요)
E="--harness ARM3 --var ARM3 --env WK_MODE=CHOP --env WK_MUA=1 --env WK_XS=1 --env WK_ARM=NONE --env WK_TICKPX=1"
python .../r1_run.py $E --tag CHK_VWOV_OFF --env WK_VWAP_OVR=0    20261030   # 회귀 (≡A)
python .../r1_run.py $E --tag CHOPX_V20    --env WK_VWAP_OVR=0.20 -j 2 20261027 20261030
python .../r1_run.py $E --tag CHOPX_V10    --env WK_VWAP_OVR=0.10 -j 2 20261027 20261030

# ② ARM 3% ONLY — 트레일링 arm 3.5% -> 3.0% (보호선 1.5% 는 그대로)
python .../r1_run.py $E --tag CHK_TARM_OFF --env WK_TRAIL_ARM=0   20261028   # 회귀 (≡A)
python .../r1_run.py $E --tag CHOPX_T30    --env WK_TRAIL_ARM=3.0 -j 3 \
    20261021 20261023 20261026 20261028 20261029

# ③ CHOP 진입 + N1 runner 승격
python .../d1_b3runner.py --tag CHOPX_A                   # B3 전수 진단 (재생 불필요)
python .../r1_run.py $E --tag CHOPX_TRBP --env WK_TREND_BYPASS=1 -j 3 <11일 전부>

# 집계 (생략일은 a4_fill 로 A 사본을 채운다)
python .../a4_fill.py --strat CHOPX --variants T30
python .../a2_metrics.py A=CHOPX_A T30=CHOPX_T30  --days ... --label "ARM 3% ONLY"
python .../a2_metrics.py A=CHOPX_A TRBP=CHOPX_TRBP --days ... --label "runner 승격"

# 10/30 H50 반사실 (재생 불필요 — evaluate_hold 는 순수 함수)
python .../b2_h50_cf.py --day 20261030 --held UP_RED --at 13:06 13:09 13:12 13:15 13:18 13:21 13:30 13:45 14:00
```

### env 대응

| env | 의미 | 기본 |
|---|---|---|
| `WK_VWAP_OVR` | 오후 XS VWAP override 의 불리폭 상한(%). 0 = OFF | 0 |
| `WK_TRAIL_ARM` | 트레일링 arm 임계값(%). 0 = OFF (production 3.5 그대로) | 0 |
| `WK_TREND_BYPASS` | 1 이면 CHOP 진입 중 `n1_adaptive.snapshot().ok` 를 TREND 로 각인 | 0 |

### 함정 (추가)

9.  **`exec_strength.mean_cttr` 는 파일 꼬리만 읽어 과거 시각을 못 본다.** 사후 분석에서 cttr 이
    전부 nan 으로 나오면 이것이다 — `c1_vwapcand._mean_cttr_full` 이 **같은 정의로** 전체를 읽는다.
10. **`small_whipsaw_hold` 의 dataclass 필드명은 `should_hold`/`should_release`/`trend`/
    `elapsed_min`** 이다(`hold`/`release`/`structural_trend`/`elapsed_minutes` 아님).
11. **진입 게이트를 완화하면 진입이 추가되지 않아도 기존 거래 손익이 바뀔 수 있다**(①의 10/27).
    거절 단계가 바뀌면서 판정기록이 달라지고 그것이 다음 진입의 regime 각인에 영향을 준다.
    게이트 연구에서 "진입 집합 diff 0" 만 확인하고 넘어가면 안 된다.
12. **B3 관리 거래의 '청산 후 틱 최고'는 승격 이득의 천장이 아니라 함정이다**(③의 10/16).
    그 두 건은 +4.93%/+4.35% 까지 갔지만 **먼저 −1.3% 손절을 맞고 나간 뒤에** 올라갔다.

---

## ⑥ 85영업일 확장 검증 (2026-11-01)

### 데이터 제약 (먼저 읽을 것)

| 데이터 | 범위 |
|---|---|
| 하이닉스/ETF 1분봉 | 105일 (05/26~10/30) → 85일 구간 = **20260624~20261030** |
| **MU 1분봉 · 체결강도** | **12일** (10/15~10/30) |

MU 가 없는 날은 MU Assist·XS·CATCH-UP 이 전부 fail-open → `A ≡ D`.
**그 동등성을 먼저 실측 검증한 뒤** B/C/D 를 11일만 돌린다(계산량 170→118회).

```bash
R=research_20261030_tick_arm3/scripts/r1_run.py
S="--env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"   # 기록전용 OFF (주문 동일성 확인됨)
B="--harness ARM3 --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 $S"
MU="20261016 20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030"

# ① fail-open 검증 — MU 없는 날에서 A 와 D 가 같아야 한다
python $R $B --tag FO_A --env WK_MUA=1 --env WK_XS=1 -j 2 20261013 20261008
python $R $B --tag FO_D --env WK_MUA=0 --env WK_XS=0 -j 2 20261013 20261008

# ② MU 없는 74일 — MUA=0 으로 1회만 돌려 A·D 공용
python $R $B --tag L85_A --env WK_MUA=0 --env WK_XS=0 -j 3 <74일>

# ③ MU 있는 11일 — 네 변형
python $R $B --tag L85_A --env WK_MUA=1 --env WK_XS=1                   -j 2 $MU
python $R $B --tag L85_D --env WK_MUA=0 --env WK_XS=0                   -j 2 $MU
python $R $B --tag L85_B --env WK_MUA=1 --env WK_XS=1 --env WK_CUKILL=1 -j 2 $MU
python $R $B --tag L85_C --env WK_MUA=1 --env WK_XS=0                   -j 2 $MU

# ④ 집계 (--fill 로 생략일에 A 산출을 쓴다)
python .../f2_regime.py  --tag L85_A                                     # regime 체제별 분해
python .../f1_monthly.py A=L85_A B=L85_B C=L85_C D=L85_D --fill L85_A    # 월별·상위제외·특정일 의존
python .../e1_entryscan.py --tag CHOPX_A                                 # 진입지표 × 결과 전수
```

### 변형 대응

| tag | MU Assist | XS | CATCH-UP | 뜻 |
|---|---|---|---|---|
| `L85_A` | ON | ON | ON | 현행 |
| `L85_B` | ON | ON | **OFF**(`WK_CUKILL=1`) | CATCH-UP 순효과 측정용 |
| `L85_C` | ON | **OFF** | ON | XS 순효과 측정용 |
| `L85_D` | **OFF** | **OFF** | **OFF** | CHOP 만 |

### 함정 (추가)

13. **`chop_regime` 은 shadow 원장 최근 10건의 h50율/tp1율로 CHOP/TREND 를 판정한다.**
    CHOP 모드의 성과는 이 라벨에 좌우된다 — TREND 각인이면 B3/P3 가 아니라 **BASE(N1) 래더**가
    관리해 TP2 8% 까지 간다. 85일 기준선을 하나의 숫자로 보면 안 되고 **체제별로 분해**해야 한다
    (06/24~08/28 은 TREND 단일 체제라 B3 가 한 번도 발동하지 않는다).
14. **MU Assist 를 켜면 재생이 3~5배 느려진다.** MU 없는 날은 `WK_MUA=0` 으로 돌려도 결과가
    같으므로(fail-open) 그렇게 돌리는 게 맞다. 기록전용 SHADOW 도 끈다.
15. **CPU 경합이 생기면 재생이 사실상 멈춘다**(2시간에 워커당 CPU 3.5분). 진전이 20분 이상
    없으면 프로세스를 죽이고 재시작한다 — `r1_run` 이 완료분을 건너뛰므로 손실이 없다.
    병렬도를 3→2 로 낮추는 편이 오히려 안정적이다.
