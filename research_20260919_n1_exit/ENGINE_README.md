# 연구 엔진 보존본 — 2026-09-18

`_research_engine_20260917` 과 같은 취지다(엔진이 사라져 앵커가 재현되지 않은 사고
재발 방지). 이번 것은 **다른 PC(회사 PC 아님) + pandas 3.0.3 + 드라이브 미러
작업트리**에서 돌리기 위해 손본 판이며, 무엇을 왜 바꿨는지 여기에 남긴다.

## 1. 원본

| 파일 | 출처 |
|---|---|
| `hengine5.py` | `safe_candidates_20260918/_engine/hengine5.py` + 아래 패치 2건 |
| `common.py` | `safe_candidates_20260918/_engine/common.py` (무수정) |
| `tregime.py` / `adaptive.py` | `_research_engine_20260917/` (무수정) |
| `hengine.py` | **신규 shim** — `common.py` 가 `import hengine` 하는데 그 이름의 모듈이 보존돼 있지 않아, 같은 모듈 상태를 공유하도록 `hengine5` 를 재노출한다 |

## 2. 패치 2건 (이게 없으면 결과가 조용히 틀린다)

### (1) `PROJECT_ROOT` 하드코딩

원본은 `C:\Users\FURSYS\Desktop\AI-GAP 2` 로 박혀 있다. 다른 PC 에서는 실제 경로로
바꿔야 한다. 이번 연구에서는 **작업트리가 아니라 HEAD `c40f5f5` export 사본**을
가리켰다 — 드라이브 동기화 지연으로 작업트리의 `kis_realized.py` 등 4개 파일이
HEAD 보다 옛 버전이라 `order_executor` import 가 깨져 있었기 때문이다
(`ImportError: cannot import name 'kis_reported_leg_cost'`).

```
git archive c40f5f5 app scripts | tar -x -C <dest>
cp config.yaml <dest>/ ; cp -r config <dest>/       # config.yaml 없으면 수수료 기본값으로 떨어진다
cp scripts/_tmp_20260907_exitlab.py <dest>/scripts/  # untracked 라 archive 에 안 담긴다
```

`ce.CACHE_DIR` 는 실제 `data/cache` 로 monkeypatch 한다(export 사본에는 데이터가 없다).

### (2) pandas 3 단위버그 — `Quotes`

`pd.DatetimeIndex(...).asi8`(pandas 3 에서 us 일 수 있음)와
`pd.Timestamp(when).value`(항상 ns)를 비교하므로, **ctx 를 새로 빌드하면** 질의값이
항상 커서 `searchsorted` 가 배열 끝을 반환하고 모든 체결가가 시계열 마지막
가격(상수)이 된다. 경고 없이 승률 83% 같은 무의미한 수치가 나온다.

```python
self.ts = pd.DatetimeIndex(w["datetime"]).as_unit("ns").asi8
i = int(np.searchsorted(self.ts, pd.Timestamp(when).as_unit("ns").value, side="right"))
```

## 3. ctx

`ctx_78d_20260527_20260918.pkl` — 78영업일. `hengine5._CTX_CACHE` 가 스크립트와 같은
폴더의 `_ctx_h50.pkl` 을 보므로, 그 이름으로 복사하면 같은 ctx 로 재현된다.

보존 ctx(`_research_engine_20260917/ctx_76d_20260527_20260916.pkl`)와 겹치는 구간이
**완전 동일**함을 확인했다 — 3분봉 10,921개 OHLCV 불일치 0, 플래그 674개 동일,
ETF 1분봉 close 불일치 0. 최근 1주일(0911~0918)은 KIS 재수신본으로 채웠고 0911~0917
은 기존 캐시와 셀 단위로 일치했다.

## 4. 회귀검증 (이 판이 기준 엔진과 같다는 증거)

W70(20260605~20260916):

```
X2-lite W1   144거래  176.3247   (공표 176.3247, 차이 +0.0000)
H50          141거래  204.1759   (공표 204.1759, 차이 +0.0000)
N1           142거래  396.6787   (공표 396.6787, 차이 +0.0000)
N1-Safe      142거래  384.4773   (공표 384.48)
```

**ctx 를 재빌드하거나 캐시를 건드린 뒤에는 반드시 이 4개를 먼저 확인하고 시작할 것.**

## 5. memo 주의

`_MEMO` 는 **봉 인덱스(p_idx)를 키로 쓴다.** ctx 의 시작일이나 봉 구성이 바뀌면
인덱스가 밀려 **캐시된 판정이 조용히 틀린 값으로 재사용된다.** 다른 ctx 로 돌릴 땐
`H._MEMO_PATH` 를 다른 파일로 두거나 memo 를 지울 것. 이번 연구는
`_ctx_B.pkl` / `_memo_B.pkl` 로 분리해 Phase A(보존 ctx)와 섞이지 않게 했다.

또한 q3(quality≥3) 변형은 `H._MEMO["base"]` 를 별도 dict 로 갈아끼운 뒤 실행하고
끝나면 되돌린다(원본 `safe_study.py` 와 동일). 이 base memo 는 디스크에 저장되지
않으므로 q3 변형은 매 실행 재계산된다(70일 기준 100~180초).

## 6. 실행 스크립트

| 파일 | 내용 |
|---|---|
| `phaseB_week.py` | ctx 재빌드 + W70 앵커 재현 + 최근 1주일 4전략 |
| `phaseD_30d.py` | 창 지정 실행 (`30` 같은 일수 또는 `20260801` 같은 시작일) |
| `phaseE_report30.py` | 요약·일별·차이거래 리포트 |
| `phaseC_report.py` | 1주일용 거래 전량 표 |
| `check_ctx_parity.py` | 재빌드 ctx vs 보존 ctx 구조 비교 |
| `compare_fetched.py` | KIS 재수신본 vs 기존 replay 캐시 값 비교 |
| `fetch_week.py` | `tw2_3slot_recent_week_fetch.py` 의 경로만 바꾼 사본 |
