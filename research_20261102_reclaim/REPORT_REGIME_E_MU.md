# REGIME_E — MU Assist 켠 현재 운영환경에서 재검정 (2026-11-02, research only)

앞선 `REPORT_REGIME_E.md` 는 MU Assist **OFF** 58일 결과다(과거 MU 데이터가 없어서).
현재 운영구성은 **CHOP + MU Assist ON + XS ON** 이므로, MU 데이터가 있는 날만으로 다시 본다.

- A = `L85_A` (현행 운영구성, 기존 산출 재사용)
- B = `REGEMU_B` — 승인 시점 `state.p3_last_regime == "TREND"` 이면 E 돌파대기 생략 (`WK_REGIME_E=1`)
- 기간 **11일 2026-10-16~10-30** (MU 1분봉·체결강도는 10/15~10/30 12일, 10/15 는 10/16 의 전일 seed 로만 사용)
- production 무수정. 격리 워크트리 `wt_arm3`(HEAD `368e4bf`) + 하네스 사본 `harness/test_zz_rege.py`

## 회귀

`WK_REGIME_E=0` 으로 REGE 하네스를 10/28 에 돌린 결과(`CHK_REGEMU`)가 `L85_A` 와
**orders 6건 / entries 2건 / brk 9건 전부 동일**. 하네스가 기준선을 바꾸지 않는다.

## 결과 (`out/rege_mu_compare.txt`)

| | A | B | Δ |
|---|---|---|---|
| 거래 | 18 | 18 | 0 |
| 총손익 | +3,299,595 | +3,390,442 | **+90,847** |
| PF | 7.341 | 7.516 | +0.17 |
| MDD | 211,393 | 211,393 | **0** |
| 최악일 | −211,393 (10/22) | −211,393 (10/22) | **0** |
| 승률 | 77.8% | 77.8% | 0 |
| 상위1/2 제외 | +2,589,051 / +1,957,450 | +2,679,898 / +2,048,297 | +90,847 / +90,847 |

regime 별: CHOP 13건 **완전 동일**, TREND 5건 +1,187,208 → +1,278,055.

**달라진 거래는 단 1건이다.**

```
20261030 13:54 DOWN_BLUE TREND
  A 14:09:40 E_BRK @7,715 → 15:00 FORCED_LIQUIDATION  +276,399
  B 14:00:00 IMMED @7,650 → 15:00 FORCED_LIQUIDATION  +367,246   Δ +90,847
```

진입가 0.84% 개선, 청산 시각·사유 동일. 형식상 채택기준(TREND 개선 + MDD/최악일 악화 없음)은 **충족**.

## 그런데 이 11일에서는 손실 쪽 다리가 한 번도 검정되지 않았다

E 돌파대기가 걸린 것은 11일 동안 **9회(7일)** 이고, `WK_REGIME_E` 판정 로그상 regime 은

| 날짜·시각 | regime | A 결과 |
|---|---|---|
| 10/16 09:51 · 10/21 09:51 · 10/28 09:45 · 10/28 12:24 · 10/29 09:57 | CHOP | E 진입 |
| 10/23 10:45 · 10/26 09:12 · 10/28 09:27 | CHOP | **E 만료(미진입)** |
| **10/30 14:00** | **TREND** | E 진입 |

- **TREND × E 교집합이 1건뿐**이다. 나머지 8건은 전부 CHOP 이라 B 가 손대지 않는다.
- 58일 검정에서 B 를 깎아내린 다리(= E 만료로 안 사던 TREND 신호를 사게 됨, 5건 −0.76M)가
  **이 구간에는 0건**이다. 만료 3건이 전부 CHOP 이기 때문이다.
- 즉 +90,847 은 "진입가 개선" 한쪽 다리만 n=1 로 측정된 값이다. 순효과가 아니다.

## MU Assist 는 E 의 모수 자체를 바꾼다 (58일 결과를 그대로 옮기면 안 된다)

같은 날 같은 코드인데 MU 유무로 **잡는 신호가 다르다**.

| 날 | MU OFF (`L85_D`) | MU ON (`L85_A`) |
|---|---|---|
| 10/23 | 10:54 CHOP + **11:45 `11:33 UP_RED` E_BRK TREND** | 10:54 CHOP + **12:33 `12:27 UP_RED` 즉시 TREND** (11:33 신호 자체가 없음) |
| 10/30 | 10:09 TREND + **12:19 `12:03` E_BRK TREND** | **14:09:40 `13:54` E_BRK TREND** 1건뿐 |

58일 결과의 10월 Δ +562,570 중 대부분(+586,354)이 10/23 의 그 11:33 E_BRK 한 건이었는데,
**현재 운영구성에는 그 거래가 존재하지 않는다.** MU-OFF 58일의 숫자를 현행 환경의 기대값으로
읽으면 안 된다.

## 결론

현행 MU Assist 운영환경에서 "TREND 진입만 E 생략"은 **MU 데이터가 있는 11일에서 +90,847 (1거래),
MDD·최악일·CHOP 거래 전부 불변**이다. 방향은 58일 검정과 같고 해로운 증거는 없다.
다만 **근거는 거래 1건**이고 손실 쪽 다리(만료 TREND 신호 매수)는 이 구간에서 한 번도 발생하지
않아 검정되지 않았다. **추가로 돈을 번다고 말할 수 있는 수준의 증거가 아니다 — 판정 보류.**

증거를 늘리려면 MU 데이터가 쌓여야 한다(현재 12일). 11월 거래일이 20일쯤 쌓여
TREND×E 교집합이 5건 이상 되면 같은 하네스로 재검정하면 된다.

## 재현

```bash
export WT=.../wt_arm3 REPLAY_SP=.../b3t_sp ARM3_RT=.../arm3_sp/rt
export SPD=<스크래치>; export ARM3_OUT=$SPD/out_rege_mu
R=research_20261102_reclaim/scripts/r1_run.py
C="--harness REGE --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
   --env WK_MUA=1 --env WK_XS=1 --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"

python research_20261102_reclaim/scripts/h1_make_rege.py
python $R $C --tag CHK_REGEMU --env WK_REGIME_E=0 20261028            # 회귀 (≡ L85_A)
python $R $C --tag REGEMU_B   --env WK_REGIME_E=1 -j 2 \
  20261016 20261021 20261023 20261026 20261028 20261029 20261030      # E arm 이 있던 7일
python research_20261102_reclaim/scripts/f1_fill_b.py \
  --a-out <arm3_sp/out> --b-out $SPD/out_rege_mu --a-tag L85_A --b-tag REGEMU_B --from-day 20261016
python research_20261102_reclaim/scripts/d3_e_regime.py compare \
  --a L85_A --b REGEMU_B --out <arm3_sp/out> --bout $SPD/out_rege_mu
```

E arm 이 없던 4일(10/19·20·22·27)은 `_e_arm_pending_instead_of_entry` 가 호출되어도 원본과
같은 값을 돌려주므로 B ≡ A 가 코드상 보장된다 → `f1_fill_b.py` 로 A 사본을 채운다.

### 함정

- `WK_OUT.rege.json` 판정 로그에는 **전일(P) 선재생분이 섞인다**(10/29 파일에 10/28 의 arm 3건이
  같이 들어온다). regime 집계 전에 `t[:10]` 으로 그 날만 걸러야 한다.
- `d3_e_regime.py` 는 `ARM3_OUT` 환경변수가 있어야 import 가 통과한다(`--out` 과 별개).
