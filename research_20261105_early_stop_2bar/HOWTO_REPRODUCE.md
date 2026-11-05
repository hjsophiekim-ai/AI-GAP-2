# 재현 — 초기 30분 손절 2봉 확인 (규칙 B)

production 무수정. 재생은 격리 워크트리(`wt_arm3` = 운영 HEAD 368e4bf) + 하네스 사본.

## 0. 경로

```bash
export WT="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_arm3"
export REPLAY_SP="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"
export ARM3_RT="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"
export ARM3_OUT="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"   # 기준선 L85_A_*.json 이 있는 곳
```

## 1. 오프라인 반사실 (재생 불필요, 1분)

```bash
python research_20261105_early_stop_2bar/scripts/s1_cf.py
```

운영구성 58일(2026-08-03~10-30) 재생 원장에서 `TIME_WINDOW_STOP_LOSS` 20건을 뽑아
1분봉으로 3분봉을 재구성해 규칙 B 를 모사한다. 출력 `out/cf_58d.csv`.

**이 결과는 경계에서 틀린다** — `research_20261103_stoploss15` §3 과 같은 이유로
재구성 봉 종가가 재생보다 약 0.05%p 낙관적이다. 실제 08/03 10:36 거래는
오프라인 −6,590(악화) vs 재생 +6,590(개선)으로 **부호가 뒤집혔다**.
이 스크립트는 "어느 날을 재생할지" 고르는 용도다.

## 2. 하네스 생성

```bash
python research_20261105_early_stop_2bar/scripts/h0_make.py
# -> research_20261105_early_stop_2bar/harness/test_zz_slh.py
```

ARM3 하네스(`research_20261030_tick_arm3`) 위에 패치 2건만 더했다.
`r1_run.py` 의 `HARNESS` dict 에 `"SLH"` 가 등록돼 있어야 한다.

### 구현 — 손절선을 다시 계산하지 않는다

진입 후 `WK_SLHOLD_MIN` 분 이내의 **첫** `TIME_WINDOW_STOP_LOSS` 집행만 1회 억제한다.
그러면 production 이 다음 완성 3분봉에서 같은 래더로 **스스로 재판정**한다.

| 다음 봉 | production 동작 | = 규칙 B |
|---|---|---|
| 종가가 손절선 이하 | 손절을 다시 집행 | CONFIRMED |
| 손절선 위로 회복 | 청산을 내지 않음 | SURVIVE |

유예 중에도 다른 청산 규칙은 그대로 돌고, `WK_SLHOLD_HARD`% 에 닿으면
`run_once` 앞단에서 틱 단위로 즉시 집행한다.

| env | 뜻 | 기본 |
|---|---|---|
| `WK_SLHOLD` | `""`/`0` = OFF · `1`/`ON` = 시간조건만 · `MU` = + 보유방향 MU DIVERGE 면 유예 금지 | OFF |
| `WK_SLHOLD_MIN` | 유예 허용 창(진입 후 분) | 30 |
| `WK_SLHOLD_HARD` | 유예 중 hard stop (net %) | −2.5 |

## 3. 측정 신뢰성 — 노브가 실제로 판정을 바꾸는가

```bash
R=research_20261030_tick_arm3/scripts/r1_run.py
B="--harness SLH --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
   --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"

# 회귀 — OFF 면 기준선과 완전 동일해야 한다
python $R $B --tag CHK_SLH_OFF --env WK_MUA=0 --env WK_XS=0 --env WK_SLHOLD=0 20260803
```

`CHK_SLH_OFF_20260803.json` 의 `orders`·`entries` 가 `L85_A_20260803.json` 과
**완전 동일**함을 확인했다(2026-11-05).

## 4. 본 재생 — 18일

대상은 기준선에 `TIME_WINDOW_STOP_LOSS` 가 있던 18일뿐이다. 나머지 40일은
유예 대상 자체가 없어 **코드상 A 와 동일**하다.

```bash
# 비 MU 16일
python $R $B --tag CHK_SLH_ON --env WK_MUA=0 --env WK_XS=0 --env WK_SLHOLD=1 -j 1 \
  20260803 20260805 20260810 20260811 20260812 20260820 20260821 20260824 \
  20260827 20260831 20260903 20260915 20260930 20261012 20261013 20261014

# MU 1분봉이 있는 2일
python $R $B --tag CHK_SLH_ON --env WK_MUA=1 --env WK_XS=1 --env WK_SLHOLD=1 -j 1 \
  20261021 20261026
```

`-j 3` 은 메모리를 많이 쓴다 — 저사양에서는 `-j 1` 로 돌린다(2026-11-05 사용자 요청).

## 5. 집계

```bash
python research_20261105_early_stop_2bar/scripts/s2_compare.py
```

## 표본 한계

- 손절 20건 / 그 중 30분 이내 14건. **상위 2건이 효과의 대부분**을 차지한다.
- **MU 상태를 아는 손절은 1건뿐**이다 — MU 1분봉이 10/19 부터만 존재한다
  (`project-mu-data-starts-1015`). "MU CONFIRM 한정" 은 이 표본으로 검정 불가.
