# 재현 방법

## 전제

- production 코드는 **수정하지 않는다**. 스크립트는 `app.trading.macd2` 를 읽기 전용 import 만 한다.
- full replay 를 돌리지 않는다. 날짜별 순차로 하루치만 메모리에 올린다.
- 기준 커밋: `d0e7bef` (main-MACD2).

## 입력 데이터 (저장소에 넣지 않았다 — 하루치 스냅샷이 16MB 다)

전부 R2 버킷 `macd2-shadow-data` 에서 받는다. `.env` 의 `R2_READ_*` 가 읽기 전용 키다.
SigV4 서명은 `ws_shadow_logger/backup.py` 의 것을 그대로 쓰고,
ListObjectsV2 는 정렬된 canonical query 로 따로 서명해야 한다.

| 경로 | 내용 | 범위 |
|---|---|---|
| `ws_shadow/YYYY/MM/DD/ws_shadow_export_<날짜>.zip` | MU 1분봉 + 000660/0193T0/0197X0 1초 스냅샷 | 2026-10-15 ~ |
| `ai-gap-2-ledger/YYYY/MM/DD/` | signal / execution / mu_assist 원장 | **2026-11-05 ~** (그 이전은 어디에도 없다) |

zip 안 경로는 `mu/<날짜>/bars_1m.csv`, `snapshots/<날짜>/000660.csv` 꼴이다.
풀어서 아래 구조로 둔다:

```
<RT_ROOT>/mu/2026-11-06/bars_1m.csv
<RT_ROOT>/snapshots/2026-11-06/{000660,0193T0,0197X0}.csv
<LED_ROOT>/20261106/macd2_{signal,execution,mu_assist}_ledger.csv
```

KIS 재생 1분봉(`replay_YYYYMMDD_{hynix,long,inverse}_1m.csv`)이 있으면 그걸 우선 쓰고,
없으면 1초 스냅샷에서 1분 OHLC 를 재구성한다.
**재구성본은 08:00 기준봉이 어긋난다** — 세션 수익률은 원장이 있는 날은 원장값을 쓴다.

## 환경변수

`scripts/wx_lib.py` 상단에서 읽는다. 생략하면 연구 당시의 로컬 경로를 쓴다.

```bash
export AXCT_REPO=/path/to/AI-GAP-2         # 저장소 루트 (sys.path 에 들어간다)
export AXCT_RT_NEW=/path/to/rt_new         # 11/03~11/06 전개본
export AXCT_RT_OLD=/path/to/rt_old         # 10/15~11/02 전개본
export AXCT_LED=/path/to/led               # production 원장 루트
export AXCT_BARS=/path/a:/path/b           # KIS 재생 1분봉 디렉터리 (os.pathsep 구분, 선택)
export PYTHONIOENCODING=utf-8              # Windows cp949 콘솔용
```

## 실행 순서

```bash
cd research_20261107_cttr_and_mu_confirm/scripts

# 연구 1 — 오후 BLUE 체결강도
python cx_scan.py        # 27건 추출 + A~D 지표  -> out_blue.csv   (약 2분)
python cx_report.py      # 지표별/그룹별 집계, 판정이 갈리는 후보 전수
python cx_stab.py        # 판정 시각 ±3분 안정성
python cx_1157.py        # 11/06 11:57 BLUE 상세 + production 패리티

# 연구 2 — MU CONFIRM 손실 공통점
python mc_1330.py        # 11/06 13:30 미진입 건 상세
python mc_scan.py        # 28건 추출 + 피처/사후경로 -> out_confirm.csv (약 2분)
python mc_cmp.py         # 손실 vs 승리 분포 비교
python mc_rank.py        # 중앙값 분할, acc2 순위표
python mc_common.py      # 손실 9건 공통조건 확인
python mh_tertile.py     # MU-HY 괴리 3등분 (out_confirm.csv 필요)
python mh_rule.py        # '오전 ∧ 괴리>=2.5' 규칙의 효과
python mh_catchup.py     # MU CATCH-UP 게이트와의 겹침 (약 3분)

# 연구 3 — 레짐 × 괴리  (앞 단계 산출물을 순서대로 먹는다)
python rg_scan.py        # 전체 플래그 135건 -> out_regime.csv            (약 6분)
python rg_fix.py         # 11/05·11/06 을 원장 기준으로 교체 -> out_regime2.csv
python rg_cmp.py         # 레짐 라벨 4종 × 괴리 상호작용
python rg_robust.py      # 교란 점검 · 날짜 LOO · 초기/후기 재검
python rg_score.py       # +2 / 0 / -1 가산점 체계 + 11/06 전수
python rg_gate.py        # 게이트 변형 V1/V2/V3 와 고원 확인
python wt_scan.py        # ENTRY_WAIT 42건 + 반대gap/체결강도 -> out_wait.csv
python wt_cmp.py         # WAIT 승자/패자 + 레짐×괴리
python wt_stack.py       # 현행 WAIT 게이트에 얹기 / 대체 / 안 하기 비교
python mu_pop.py         # CONFIRM+WAIT 70건 + 왕복 지표 6종 -> out_mu.csv  (약 3분)
python mu_chop.py        # 왕복장 기준 6종 비교
python mu_block.py       # 차단 규칙 견고성(LOO·치환) + 11/06 적용
python mu_final.py       # 기존안 vs 신안 최종 비교
```

`rg_scan.py` → `rg_fix.py` → (`wt_scan.py`, `mu_pop.py`) 순서는 **지켜야 한다**.
`mh_*` 는 `out_confirm.csv`, `wt_*`·`mu_*` 는 `out_regime2.csv` 를 cwd 에서 읽는다
(`data/` 에서 복사해 두고 돌리면 된다).

`cx_scan.py` / `mc_scan.py` 는 cwd 에 CSV 를 쓴다. `data/` 의 사본과 byte 단위로 같아야 한다:

```bash
diff out_blue.csv ../data/out_blue.csv
diff out_confirm.csv ../data/out_confirm.csv
```

나머지 스크립트의 stdout 은 `data/<이름>_out.txt` 로 보관돼 있다.

## 함정 (다시 밟지 말 것)

- `exec_strength.mean_cttr` 은 **파일 끝 256KB 만** 읽는다(라이브 append 전제).
  과거 시각에 부르면 조용히 `None` 이다. `cx_lib.DaySnap.mean_cttr` 이 같은 정의로 전체를 스트리밍한다
  (장 마감 직전 시각에서 production 값과 일치함을 `cx_1157.py` 가 대조한다).
- production 의 "rolling 60초"는 **누적값의 60초 평균**이지 단기 흐름이 아니다.
- 스냅샷 재구성 1분봉의 08:00 기준봉은 전일 종가에 가깝다.
  11/06 에서 `hy_session_pct` 가 1.051(원장) 대 7.156(재구성)으로 벌어진다.
- `cttr == 100 × buy_vol_cum / sell_vol_cum` 이 스냅샷에서 정확히 성립한다 —
  창 체결강도는 cum 증분으로 계산하면 되고 새 공식이 아니다.
