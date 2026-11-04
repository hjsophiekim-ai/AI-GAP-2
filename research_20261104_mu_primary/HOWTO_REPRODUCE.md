# 재현 방법 — MU 플래그 원신호 전략

## 전제

- **production 코드 무수정, READ-ONLY.** 재생은 격리 워크트리 + 하네스 **사본**으로만 돌린다.
- 워크트리: `C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_arm3` = 운영 HEAD `368e4bf`
- 하네스: `harness/test_zz_mupri.py` — `scripts/h1_make_mupri.py` 가 ARM3 하네스
  (`research_20261030_tick_arm3/harness/test_zz_arm3.py`)에 **블록 하나만 덧붙여** 만든다.
  anchor 가 정확히 1개가 아니면 생성 자체가 실패한다.

## 환경변수

```bash
export WT=C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_arm3
export REPLAY_SP=C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp
export ARM3_RT=C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt
export ARM3_OUT=C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out
```

데이터는 기존 연구가 받아둔 것을 그대로 쓴다(새로 받을 필요 없음):
`{ARM3_RT}/mu/<날짜>/bars_1m.csv` · `{ARM3_RT}/snapshots/<날짜>/000660.csv` ·
`{REPLAY_SP}/data/replay_<YYYYMMDD>_{hynix,long,inverse}_1m.csv`

## 1) MU 플래그 전수 + 판정기 부착 (재생 불필요)

```bash
python research_20261104_mu_primary/scripts/m1_flags.py     # -> out/mu_flags.csv
python research_20261104_mu_primary/scripts/m2_select.py    # 게이트별 단변량 대조
python research_20261104_mu_primary/scripts/m3_combo.py     # 사전 지정 조합 + S2 전수
```

전부 production 함수를 직접 호출한다 — `mu_assist.load_mu_1m` / `_macd_hist` /
`evaluate_pattern`, `exec_strength.state_for`, `signal_engine.resample_completed_3m`,
`time_window_filter._gap_series` / `_count_recent_confirmed_crossovers`,
`major_flag_filter.compute_component_scores` / `score_for_direction`, `teg_gate.evaluate_teg`,
`trend_persistence_filter.evaluate_trend_persistence`, `chop_defense._trend_ok` / `_path_is_chop`.

## 2) 하네스 생성 + 회귀 확인

```bash
python research_20261104_mu_primary/scripts/h1_make_mupri.py

R=research_20261104_mu_primary/scripts/r1_run.py
B="--harness MUPRI --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
   --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"

# 변형 OFF 면 기존 기준선(L85_A)과 주문이 바이트 단위로 같아야 한다
python $R $B --tag CHK_MUPRI_OFF  --env WK_MUA=1 --env WK_XS=1 \
    --env MUPRI_PLAN=$ARM3_OUT/mupri_plan_S2.json 20261019
python $R $B --tag CHK_MUPRI_OFF2 --env WK_MUA=1 --env WK_XS=1 \
    --env MUPRI_PLAN=$ARM3_OUT/mupri_plan_S2.json 20261027
```

## 3) 진입 계획 생성 + 본 재생

```bash
python research_20261104_mu_primary/scripts/m4_plan.py S2        # -> {ARM3_OUT}/mupri_plan_S2.json
python research_20261104_mu_primary/scripts/m4_plan.py B_USER    # 사용자 원안

D="20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030"
MU="--env WK_MUA=0 --env WK_XS=0"      # MU Assist/XS/CATCH-UP 중복 적용 금지

python $R $B $MU --tag MUPRI_M1_S2 --env WK_MUPRI=M1 \
    --env MUPRI_PLAN=$ARM3_OUT/mupri_plan_S2.json -j 2 $D
python $R $B $MU --tag MUPRI_M2_S2 --env WK_MUPRI=M2 \
    --env MUPRI_PLAN=$ARM3_OUT/mupri_plan_S2.json -j 2 $D
python $R $B $MU --tag MUPRI_BUSER --env WK_MUPRI=M1 \
    --env MUPRI_PLAN=$ARM3_OUT/mupri_plan_B_USER.json -j 2 $D
```

## 4) 집계

```bash
python research_20261104_mu_primary/scripts/m5_compare.py A=L85_A M1=MUPRI_M1_S2 M2=MUPRI_M2_S2
python research_20261104_mu_primary/scripts/m6_states.py  MUPRI_M1_S2 S2 MUPRI_M2_S2 S2
```

## env 대응

| env | 의미 | 기본 |
|---|---|---|
| `WK_MUPRI` | `M1` = MU 원신호 + HY 게이트 우회 / `M2` = HY 게이트 유지 / 미설정 = 원본 ARM3 | 미설정 |
| `MUPRI_PLAN` | MU 진입 결정시각 목록 JSON (`m4_plan.py` 산출) | — |

---

## 함정 (이 연구에서 실제로 밟은 것)

1. **MU 1분봉 10/15·10/16 은 구 로거 포맷이라 못 쓴다.** `feed` 가 숫자/체결강도 값이고
   행 대부분이 `minute_kst=20:26` 중복이다. `load_mu_1m` 이 `None` 을 반환하므로
   그 두 날은 MU 가 **없는 것과 같다**. 사용 가능 구간은 **10/19~10/30 10일**이다.
2. **재생은 전일(P)·전전일(P2)을 먼저 돌린다.** 진입 주입을 날짜로 거르지 않으면
   전일 재생 구간에서 플래그가 소진돼 본 날에 하나도 안 들어간다
   (`_nk.strftime("%Y%m%d") == os.environ["WK_D"]` 가드 필수).
3. **하이닉스 진입을 `approved=True` 일 때만 막으면 샌다.**
   `_resolve_tw2_3slot_candidate_body` 의 `window_blocked_by_morning_only` 가
   "오후라서 거절(`TW_REJECT_TIME_WINDOW`)" 된 후보를 **승인 여부와 무관하게** 다시 통과시킨다.
   이 누수로 M2 에 HY 진입 2건(+113,645 · +117,565)이 섞여 들어갔다.
   **`block_reason == config.TW_REJECT_TIME_WINDOW` 도 같이 바꿔야 한다.**
4. **`exec_strength.mean_cttr` 는 파일 꼬리만 읽어 과거 시각을 못 본다.** 사후 분석에서
   cttr 이 전부 `UNAVAILABLE` 로 나오면 이것이다 — `m1_flags.mean_cttr_full` 이
   **같은 정의로** 전체를 읽는다.
5. **신호원장에는 한 MU 후보가 여러 행으로 남는다.** CHOP 모드는 승인 후 E 돌파대기를 타므로
   `...:TW2_3SLOT_CONFIRM` 행이 `WAITING` 으로 남고 실제 체결은 `...:E_BRK` 접미사 행이다.
   `signal_id` 접두사로 묶어 **최종 상태 하나**만 세야 한다(안 그러면 체결 17건이 2건으로 보인다).
6. **`gates`/`sig` 로그에 전일분이 섞인다.** 집계 전에 `t[:10]` / `detected_at[:10]` 로 거른다.
7. **`trend_persistence_filter.evaluate_trend_persistence` 는 위치인자다**
   (`bars_3m, df_1m, flag_direction, now`). 키워드 `direction=` 은 TypeError 다.
   그리고 `df_1m` 은 판정 시각까지 잘라서 넣어야 한다 — 안 자르면 미래를 본다.
8. **`teg_gate.evaluate_teg` 는 `flag_bar_dt` 가 프레임 마지막 바로 앞 행이어야 한다.**
   아니면 `flag_bar_not_one_before_confirm_bar` 로 끝난다. MU 판정에 붙일 때는
   마지막 두 3분봉을 (flag, confirm) 으로 준다.
9. **재생 1일이 3~7분 걸리고 CPU 경합에 매우 민감하다.** 병렬도를 3 이상으로 올리면
   오히려 진전이 멈춘다. `r1_run` 이 완료분을 건너뛰므로 죽여도 손실은 없다.
