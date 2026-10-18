# 재현 방법 (2026-10-16 연구 3건)

## 전제

- production 코드·config 는 **수정하지 않는다**. 재생 하네스 안의 monkeypatch 만 쓴다.
- 기준 워크트리: `<SP4>/wt` (커밋 `59171b8`). `<SP4>` =
  `C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad`
- 1분봉: `<SP4>/bb/sp/data/replay_{YYYYMMDD}_{hynix|long|inverse}_1m.csv`
  (하이닉스는 08:00 프리마켓 포함 — 빼면 그날 재생이 통째로 달라진다).
- 작업 폴더 `<W>` = 이 세션 스크래치패드의 `wk/`. 원장은 `<W>/out/{TAG}_{YYYYMMDD}.json`.

## 0) 하네스

```bash
cp <SP4>/bb/test_zz_tr2.py <SP4>/wt/tests/macd2/test_zz_wk22.py   # BASE 용 (원본 그대로)
python scripts/mkharness.py                                        # -> <SP4>/wt/tests/macd2/test_zz_v2.py
```

`mkharness.py` 가 더하는 것은 네 군데 삽입뿐이고 전부 env 로 꺼진다.

| env | 뜻 |
|---|---|
| `PMV=RELAX` | 13:30~14:30 DOWN_BLUE 에 한해 TW2 VWAP veto −1.0%→−2.0%, TEG `COND_VWAP` 여유 0%→1.0% |
| `PMV=OFF` | 같은 창에서 두 VWAP 조건 비적용 |
| `WHIP_ON=hhmm` `WHIP_K=k` | 그 시각~11:00 에 오전 진입이 이미 k 번이면 신규진입 차단(슬롯 미소비) |

**OFF parity 확인**: env 없이 `test_zz_v2.py` 로 20260609 을 돌리면 `BASE_20260609.json` 과
`orders/sig/acts/entries/ex` 가 완전히 같아야 한다 (`PARITY_20260609.json` 로 확인함).

## 1) BASE (production 현행 = E + SLOW-TP2 ON) 89일

```bash
cat <W>/jobs_base.txt | xargs -P 4 -I{} bash -c 'set -- {}; <W>/job.sh "$1" "$2" "$3" "$4"'
```

`job.sh` 가 주는 env: `WK_FILL=REAL WK_VAR=UPFASTRS MACD2_SLOW_TP2_ENABLED=true`,
`REPLAY_SP=<SP4>/bb/sp`, `RS_TABLE=<SP4>/bb/rs_table.json`, `WK_D/WK_P/WK_P2`.
`WK_P/WK_P2` 는 **직전·전전 영업일**이어야 한다(공휴일 건너뛰기). 1일 ≈ 3.5분(단독) /
≈10분(4~6 병렬, 4코어).

## 2) 연구 1 (재생 불필요 — 관측만)

```bash
python scripts/s1_mpeak.py   > out/s1_out.txt     # 오전고점·되밀림 x PM-BLUE 교차표
python scripts/s1b_auc.py    > out/s1b_out.txt    # 임계값 없는 AUC/순위상관/사분위
```

## 3) 연구 2

```bash
python scripts/s2_screen.py  > out/s2_screen_out.txt   # 차단사유별 사후수익 (기회 크기)
python scripts/s2_days.py                               # 3분봉 VWAP 편차 -> 영향 가능일
#   PMOFF   = 차단사유 VWAP_VETO  또는 (TEG 차단 & dev3m<0)        -> 28일
#   PMRELAX = (TEG 차단 & -1.0<=dev3m<0)                           -> 7일
#   그 외 사유(슬롯캡/미확정/MACD gap/H50)는 VWAP 조건보다 앞/뒤라 정의상 무영향
cat <W>/jobs_PMOFF.txt <W>/jobs_PMRELAX.txt | xargs -P 2 -I{} bash -c 'set -- {}; <W>/jobv.sh "$1" "$2" "$3" "$4"'
python scripts/rep.py "연구2 PM-BLUE VWAP VETO" PMRELAX PMOFF > out/s2_replay_out.txt
```

## 4) 연구 3

```bash
python scripts/s3_whip.py    > out/s3_whip_out.txt      # 오전 휩쏘일 + ON 시각 -> <W>/whipdays.txt
cat <W>/jobs_W1.txt | xargs -P 2 -I{} bash -c 'set -- {}; <W>/jobv.sh "$1" "$2" "$3" "$4" "$5"'
python scripts/rep.py "연구3 MORNING-WHIP SLOT SAVE" W1 > out/s3_replay_out.txt
```

휩쏘일 판정은 production `chop_defense.morning_no_trade` 의 ON 조건을 그대로 옮긴 것이다
(09:00~10:30 원 플래그 방향전환 ≥2 **AND** 최근 60분 PE<0.20 & EMA20 교차 ≥3).
**새 임계값을 만들지 않았다.** ON 시각은 오프라인 사전계산이다 — 규칙 입력(1분봉 + 원 플래그)이
A/B 에서 동일하므로 MC-DELAY 연구(2026-10-11)와 같은 근거로 타당하다.

## 주의

- `rep.py` 는 변형 원장이 없는 날에 BASE 원장을 그대로 쓴다. 영향 가능일만 재생했기 때문이고,
  OFF parity 가 확인됐기 때문에 성립한다.
- 이 PC 에는 10/09 이후 1분봉이 없다. 10/12·10/13·10/16 은 플래그 관측에만 쓰고 재생에서 제외했다.
- `pytest tests/macd2` 전체를 돌리면 드라이브 충돌 사본("… (1).py")까지 수집돼 실패가 부풀려진다.
  여기서는 `tests/macd2/test_zz_*.py` 한 파일만 지정해 돌린다.

## 보존된 산출물 (2026-10-17 중단 시점)

- `out/ledgers_20261016.zip` — 재생 원장 147개
  (`BASE_*` 89일 / `W1_*` 22일 / `PMOFF_*` 28일 / `PMRELAX_*` 7일 / `PARITY_20260609`)
- `out/final_replay_out.txt` — BASE vs 세 변형 전체 비교 (`rep.py` 출력)
- `harness/test_zz_wk22_base.py` — BASE 용 하네스 원본(= `bb/test_zz_tr2.py`)
- `harness/test_zz_v2.py` — `mkharness.py` 가 만든 변형 하네스 (실제로 쓴 바로 그 파일)
- `harness/{days,whipdays,jobs_*}.txt`, `harness/{job,jobv}.sh` — 실행에 쓴 날짜·작업 목록

원장만 있으면 재생 없이 `scripts/rep.py` 로 모든 표를 다시 만들 수 있다.
zip 을 `<W>/out/` 에 풀고 `anal.MY` 경로만 맞추면 된다.

## 돌리다 만 것 (의도적으로 안 돌림)

- **W2 (오전 3번째 진입만 차단, K=2)** — 89일 중 휩쏘 ON 이후 오전 3진입이 있었던 날이
  08/21 하루뿐이라 W1 과 거의 같은 규칙이 된다. 돌릴 가치가 없다고 판단했다.
- **TEG `price_ema_stack_aligned` 완화** — 연구 2의 결론이 "진짜 관문은 여기" 로 나왔지만
  사용자가 요청한 범위(VWAP veto)가 아니고, 선행연구(`research_20260920_entry_filter`)가
  "VWAP veto 가 거른 플래그를 전부 넣으면 전 구간 악화" 로 이미 부정적이다.

## 운영 중 겪은 사고 (다음에 같은 실수 안 하려고 적어둠)

1. **G: (구글 드라이브 미러) 가 장중에 연결 해제되면 재생 작업이 통째로 죽는다.**
   재생 자체는 C: 로컬 데이터만 쓰지만, 작업을 띄운 셸의 cwd 가 G: 라서 같이 죽었다.
   → 작업은 `cd <W>` (로컬)에서 띄우고, 결과도 로컬에 쓴 뒤 마지막에 G: 로 복사한다.
2. **작업 관리자를 죽여도 `xargs` 와 그 아래 python 은 살아남는다.**
   그 상태로 재시작하면 같은 날을 두 프로세스가 동시에 돌면서 `--basetemp` 가 충돌하고
   (`Device or resource busy`) rc=127/1 로 조용히 실패한다. 4코어에 12~14개가 물려
   처리량이 1/3 로 떨어지기도 했다.
   → 재시작 전에 반드시 `xargs.exe` 를 먼저 죽이고, 그 다음 `python.exe` 를 죽인다.
3. 두 풀이 같은 로그 파일(`resume_run.log`)에 쓰면 섞인다. 풀마다 로그를 분리할 것.
