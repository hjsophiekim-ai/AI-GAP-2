# 2026-10-18 R2 실시간 로거 품질 점검 + MU 수집 구조 결함

**READ-ONLY 조사.** production trading logic(`app/`) 무수정. 수정은 `ws_shadow_logger/` 3줄뿐이다.

## 1. R2 접근

`.env` 의 `R2_READ_*` (readonly) 로 ListObjectsV2 + manifest GET 만 한다. 다운로드는 조사용 2일치.
버킷 `macd2-shadow-data`, prefix `ws_shadow/`, 경로 `<PREFIX>YYYY/MM/DD/ws_shadow_export_<day>.zip`.

## 2. 쌓인 양과 품질 — 2일치뿐

| 날짜 | zip | 총 행수 | 스냅샷 | MU tick | MU 분봉 |
|---|---|---|---|---|---|
| 2026-10-15 | 2.7MB | 21,565 | 15,633 | 4,617 | 703 |
| 2026-10-16 | 12.4MB | 86,099 | 80,436 | 3,130 | 1,536 |

국내 스냅샷(1초):

| 날짜 | 종목 | 행수 | 누락률 | stale | 최대공백 |
|---|---|---|---|---|---|
| 10/15 | 000660 / 0193T0 / 0197X0 | 각 5,211 | **80.7%** | 0 / .0006 / .0031 | 1s |
| 10/16 | 000660 / 0193T0 / 0197X0 | 각 26,812 | **0.7%** | 0 / **.1085** / **.1129** | 49s |

- **10/15 는 누락 80.7% 로 쓸 수 없다**(로거 첫 가동일).
- **10/16 은 누락 0.7% 로 양호**하나 ETF 두 종목 stale 11% 는 1초 단위 분석에서 제외해야 한다.
- 사전 고정 사양이 요구하는 이벤트 30~50건에 한참 못 미친다 → **성능 판정 금지(지시대로 미실시)**.
- 누락률 5% 미만 날이 10~15 영업일 쌓이려면 10/16 수준 품질 유지 시 **약 3주**.

## 3. MU 수집 구조 결함 — 판정 **D. parser 자체 오류**

`C(수집 안 됨)` 가 아니다. **MU 는 한국 장중에 정상 수신되고 있었다.**

### 원인

`ws_shadow_logger/schema.py` 의 `OVRS_CCNL` 이 공식 `HDFSCNT0` 의 **1번 필드 `RSYM`** 을
빠뜨려 폭이 **25**(실제 26)였다. 파서는 고정폭 슬라이스를 하므로
(`recs = [dict(zip(cols, vals[i*w:(i+1)*w]))]`) **건수>1 프레임에서 레코드 i 가 i 칸씩 밀린다.**

| 밀림 | SYMB(=feed) ← | KYMD/KHMS ← | 결과 |
|---|---|---|---|
| i=0 | RSYM | XHMS(6자리) | `kst_ts=None` → **통째로 버려짐** |
| i=1 | 직전 레코드 MTYP | XYMD / XHMS | feed="2", **시각이 ET** |
| i=2 | 직전 레코드 STRN | TYMD / XYMD | feed="999.99" 등, 시각 깨짐 |

실측 12개 필드가 이 예측과 전부 일치한다 (i=1: `XYMD←ZDIV=4`, `XHMS←TYMD=20261016`,
`PASK←RATE=−1.01`, `VBID←PBID=921.60`, `EVOL←VBID`, `TVOL←VASK` …).

### 실측 (10/16)

| 항목 | 값 |
|---|---|
| MU tick 총 | 3,130건 |
| **한국 장중(09:00~15:30 KST) 수신** | **3,130건 = 100%** (09시 404 / 10시 280 / 11시 181 / 12시 223 / 13시 760 / 14시 946 / 15시 336) |
| feed="2" 1,291건 | 수신 13:00~15:29 KST ↔ 저장 `kst_ts` 00:00~02:29 — **정확히 13시간 차 = KST−ET** |
| feed 에 문자 포함 | **0건** (RSYM 이 한 번도 안 들어옴 = 레코드 0 전부 유실) |
| 고유 feed 값 | **1,388개** (전부 가격·체결강도 숫자) |

→ `00:00~02:29` 는 **미국 동부 현지시각(XHMS)을 "KST" 로 잘못 찍은 것**이다.
UTC 해석 오류도 timezone 변환 오류도 아니고 **컬럼 오인**이다.
RBAQMU/DNASMU 구분은 불가능했다 — `SYMB` 는 두 피드 모두 `"MU"` 라 그걸 쓰면 합쳐진다.

## 4. 수정 (`feature/ws-logger-mu-rsym` → main-MACD2 `f0cb468`)

```
schema.py : OVRS_CCNL 앞에 "RSYM" 추가 (25 → 26)
mu.py     : feed 를 RSYM 에서 읽는다 (SYMB 는 둘 다 "MU")
stub.py   : 테스트 프레임도 공식 26필드로
```

> **stub 이 같은 결함을 공유하고 있어서 기존 테스트가 전부 통과했다.** 그래서 stub 부터 고쳤다.

`tests/test_mu_rsym.py` 신규 5건: 폭 26 / 다건 프레임 드리프트 없음(5레코드 전부 동일) /
`kst_ts` 가 ET 아닌 KST(차이 정확히 13시간) / RBAQMU·DNASMU 분봉 분리 / 레코드 0 보존.

### 회귀

| | baseline `c91cead` | feature `836e686` |
|---|---|---|
| `ws_shadow_logger/tests` | 9 failed / **78** passed | 9 failed / **83** passed |
| 실패 목록 | `test_render_start.py` 9건 | **동일한 9건** |
| **신규 실패** | — | **0** |

`test_render_start.py` 9건은 배포 감독 스크립트 테스트로 **수정 전에도 동일하게 실패**한다
(Windows 프로세스 감독 환경 의존). 국내 TR 정의(`H0STCNT0/H0UNCNT0/H0STASP0/H0UNASP0`)는
건드리지 않았고 `test_isolation.py` 도 통과한다.

### 수정 후 저장 형태

| | 수정 전 | 수정 후 |
|---|---|---|
| `ticks.csv` `feed` | `2`,`999.99`,`100.00` … 1,388종 | **`RBAQMU` / `DNASMU`** |
| `kst_ts` | ET 현지시각(13시간 틀림) | **KST**(`KYMD`+`KHMS`) |
| `local_ymd/hms` | `MU`,`4` 쓰레기 | ET 현지 일자·시각 |
| `bars_1m.csv` `minute_kst` | `00:00~02:29` | **`09:00~15:29`** — 하이닉스 스냅샷과 같은 분으로 join |

## 5. 과거 데이터 복구

**`ticks.csv` 로는 복구 불가** — 그 단계에서 이미 필드가 유실됐다. **raw frame 이 있어야 한다.**
로거는 `raw/YYYY-MM-DD/frames_HH.txt(.gz)` 에 원문을 보관하지만 **export zip 에는 raw 가 빠진다**
(그래서 R2 에도 없다). Render Persistent Disk 에 남아 있으면 복구 가능하다.

`scripts/recover_mu.py` — 원본을 **읽기 전용**으로만 열고 `--out` 아래에만 쓴다.

```
python -m research_20261018_rt_quality.scripts.recover_mu \
    --raw-root <KIS_LOGGER_DATA_DIR 사본> --day 2026-10-16 --out <출력폴더>
```

합성 raw(다건 프레임 24개 = 72 레코드)로 검증 완료:
`feed {'RBAQMU': 36, 'DNASMU': 36}`, 장중 tick 72건, 분봉 `09:00` / `13:30` 로 정상 생성.

## 파일

- `scripts/r1_list.py` → R2 목록·manifest (키 미출력), `out/r1_manifests.json`
- `scripts/r2_get.py` → export zip 다운로드
- `scripts/recover_mu.py` → raw frame 재파싱 복구 (research/recovery only)
