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
```

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
