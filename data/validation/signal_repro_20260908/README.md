# production 신호 재현성 — 원인규명 · 아카이브 설계 · 입력방식 A/B/C 비교
### 2026-09-08, READ-ONLY

**production 무수정 / commit·push 없음.** `git status --porcelain -- app/` 클린, HEAD `5506380`.
이 문서는 **설계안**이며 코드는 한 줄도 바꾸지 않았다.

---

## 0. 헤드라인

1. **B(KIS 사후조회 premarket) 재현율 42.9%, C(정규장만) 0.0%.**
2. **A(production live premarket)는 로컬에서 측정 자체가 불가능하다** — 그 원본이
   어디에도 저장되지 않기 때문이다. 그리고 사후조회 데이터에서 프리마켓 시작시각을
   1분 단위로 전수 이동시켜도(61가지) **정답을 재현하는 조합은 존재하지 않았다.**
3. 따라서 **재현율 100%로 가는 유일한 길은 live 프레임을 그대로 아카이브하는 것**이고,
   §3~§4 에 그 설계를 담았다.
4. 이 문제가 닫히기 전까지 TW2/TWF/B(CHOP 재게이트) 수익률 연구는 재최적화하지 않는다.

---

## 1. 정답 — 2026-09-08 production 신호원장

| 플래그봉 | 방향 | T+3 판정 | 결과 |
|---|---|---|---|
| 08:27 → 08:30 | UP_RED | 08:30 | **BLOCKED / BEFORE_SESSION_OPEN** (주문 없음) |
| 09:03 → 09:06 | DOWN_BLUE | 09:09 | **재확인 승인** → 09:09:19 인버스 1,389주 @6,085 limit |
| 09:12 → 09:15 | UP_RED | 09:18 | **재확인 승인** → 09:18:12 레버리지 704주 @11,640 limit |
| 10:06 → 10:09 | DOWN_BLUE | 10:12 | **재확인 승인** → 10:12:38 인버스 1,442주 @5,730 limit |

거래원장(체결):
09:09:19 인버스 매수 @6,065 → 09:15:20 손절 @5,900 (−236,639원) →
09:18:17 레버리지 매수 @11,635 → 10:12:24 매도 @11,760 (+80,588원) →
10:12:44 인버스 매수 @5,720 (보유중)

---

## 2. 입력방식 A / B / C 재현율 비교

채점: **플래그 4건**(봉시각+방향 완전일치) + **T+3 3건**(판정시각+방향+승인여부 완전일치) = 7점 만점.
`08:30`은 장전차단이라 게이트 판정 자체가 없어 T+3 채점에서 제외했다.

| 방식 | 플래그 일치 | 오탐 | T+3 일치 | **재현율** |
|---|---:|---:|---:|---:|
| **A** production live premarket | — | — | — | **측정불가** (로컬에 원본 없음) |
| **B** KIS 사후조회 premarket | 2/4 | 4건 | 1/3 | **42.9%** |
| **C** 전일 정규장 warm-up + 당일 09:00~ 정규장만 | 0/4 | 2건 | 0/3 | **0.0%** |

### B 상세 (현재 모든 백테스트가 쓰는 입력)

```
플래그  08:15 UP_RED ✗   08:21 DOWN_BLUE ✗   08:24 UP_RED ✗
        09:03 DOWN_BLUE ✓  09:09 UP_RED ✗   10:06 DOWN_BLUE ✓
T+3     09:09 DOWN_BLUE → 거절 REJECT_MACD_GAP_NOT_EXPANDING  ✗(정답=승인)
        09:15 UP_RED    → 승인                                 ✗(정답=09:18)
        10:12 DOWN_BLUE → 승인                                 ✓
```
- 프리마켓에서 **오탐 3건**(08:15/08:21/08:24), 정답의 08:27은 못 잡음
- UP_RED 플래그가 **1봉(3분) 빠름** → 진입가 11,480 vs 실제 11,635
- 09:09 T+3 판정이 **승인/거절로 뒤집힘** → 인버스 진입·손절 사이클이 통째로 누락

### C 상세

```
플래그  09:03 UP_RED ✗   10:03 DOWN_BLUE ✗
T+3     09:09 UP_RED → 승인 ✗   10:09 DOWN_BLUE → 거절 TW2_REJECT_VWAP_VETO ✗
```
프리마켓을 빼면 EMA 시드가 완전히 달라져 **방향까지 반대**로 나온다(09:03을
production은 DOWN_BLUE, C는 UP_RED). 재현 관점에서 최악이다.

### A 역추정 — 사후데이터로는 복원 불가

당일 프리마켓 시작시각을 08:00~09:00 사이 **1분 단위 61가지**로 이동시키며 전수 탐색:

| | |
|---|---|
| 완전 일치(7/7) 컷오프 | **없음** |
| 최고 점수 | 시작 08:10 → 플래그 2/4, 오탐 6, T+3 2/3 |

시작점이 3분만 달라져도 당일 플래그가 통째로 바뀐다:

| 당일 데이터 시작 | 당일 플래그 |
|---|---|
| 08:00~ (=B) | 08:15 UP / 08:21 DOWN / 08:24 UP / 09:03 DOWN / **09:09 UP** / 10:06 DOWN |
| 08:12~ | … / 09:00 DOWN / **09:12 UP** / 10:06 DOWN |
| 08:18~ | … / 09:00 DOWN / **09:09 UP** / 10:06 DOWN |
| 08:24~ | 08:24 UP / 09:03 DOWN / 09:09 UP / **10:03 DOWN** |
| 09:00~ (=C) | **09:03 UP / 10:03 DOWN** |

**워밍업 길이는 무관하다** — 전일 1·2·3·5·12·68일 전부 동일한 결과. 갈리는 것은
오직 프리마켓 봉 집합이다.

### 왜 이렇게 민감한가

당일 gap(macd−signal) 시리즈에서 크로스 판정 지점이 **0 바로 위**에 있다:

```
08:12  −508.80      08:21   −84.84      09:03  −237.90
08:15   +35.76      08:24   +20.06 ←    09:06   −68.46
08:18  +220.85      08:27   +57.68      09:09  +138.36 ←
```

`evaluate_macd_crossover`는 `previous_diff <= 0 and current_diff > 0` 로 판정하므로
`08:24 +20.06` / `09:09 +138.36` 의 부호가 뒤집히면 플래그가 다음 봉으로 밀린다.
production 에서는 이 둘이 음수였다 — 즉 production 이 본 프레임의 EMA 가 미세하게
낮았다는 뜻이고, 그 차이를 만드는 것은 프리마켓 봉 몇 개다.

### 검증된 것 — 데이터·비용엔진은 정확

| 실제 체결 | 로컬 1분봉 |
|---|---|
| 09:09:19 인버스 @6,065 | 09:09 종가 6,060 |
| 09:15:20 인버스 @5,900 | 09:15 종가 5,885 |
| 09:18:17 레버리지 @11,635 | 09:18 시가 11,630 |
| 10:12:24 레버리지 @11,760 | 10:12 종가 **11,760 (완전일치)** |
| 10:12:44 인버스 @5,720 | 10:12 종가 5,715 |

`TradeCostEngine` 도 정확: 11,635→11,760 × 704주 = **80,588.5원**, 실제 원장 80,588원.

**즉 결함은 백테스트 로직도 가격데이터도 비용모델도 아니고, 오직 "MACD 입력 프레임"이다.**

---

## 3. 원인 메커니즘 (코드 근거)

| 위치 | 사실 |
|---|---|
| `market_data.py:843` `get_history_df()` | `self._df_1m.copy()` — **히스토리 프레임은 메모리에만 존재**. 디스크에 저장되지 않는다 |
| `market_data.py:826` `merge_incremental_1m()` | 매 사이클 `_fetch_minute_candles(..., count=10, hour1="")` — **최근 10개 1분봉만** 가져와 merge |
| `market_data.py:628` `bootstrap()` | 시작 1회만 당일 전체를 페이지 워크. **worker 시작 시각에 따라 프리마켓 커버리지가 달라진다** |
| `worker.py:4381` | `df_1m = market_data.get_history_df()` → `resample_completed_3m` → `filter_complete_3m_bars` |

결론: worker 가 08:27 시점에 실제로 보유한 1분봉 집합은 **그날 그 프로세스의
시작시각·네트워크 성패에 의존**하며, 어디에도 남지 않는다. Render 재시작이 있었다면
더더욱 복원 불가다. 사후 KIS 조회는 "지금 KIS가 갖고 있는 최종본"이라 다르다.

---

## 4. 설계 ① — live 1분봉 원본 아카이브

### 4-1. 설계 원칙

단순 "하루 끝 스냅샷"으로는 부족하다. 필요한 것은 **"시각 T 에 worker 가 무엇을
보고 있었는가"** 이므로, 각 1분봉이 **언제 프레임에 처음 나타났는지**를 함께 남겨야
한다. 그래야 나중에 `first_seen_at <= T` 로 필터링해 그 시점 프레임을 그대로 복원할
수 있다.

### 4-2. 신규 파일 `app/trading/macd2/bar_archive.py` (신규, 기존 파일 무수정)

```python
"""live 1분봉 프레임 아카이브 — production 신호 재현 전용.

MarketDataService._df_1m 은 메모리에만 존재해 재시작/재배포와 함께 사라진다.
사후 KIS 조회로는 복원되지 않음이 2026-09-08 실측으로 확인됐다
(data/validation/signal_repro_20260908/README.md).
이 모듈은 그 프레임을 관측시각과 함께 append-only 로 남긴다.
쓰기 실패는 절대 거래 경로를 막지 않는다(전부 swallow + 카운터).
"""
```

| 항목 | 값 |
|---|---|
| 저장 경로 | `data_paths.DATA_ROOT / "bar_archive" / f"hynix_1m_{YYYYMMDD}.csv"` — **Render Persistent Disk 위**(AI_GAP_DATA_DIR 하위)여야 한다 |
| 형식 | append-only CSV, 1분봉 1행 |
| 크기 | 1일 최대 ~390행 × ~120B ≈ **50KB/일**, 1년 ≈ 12MB |

**스키마**

| 컬럼 | 설명 |
|---|---|
| `datetime` | 1분봉 시각 (KST ISO) — 키 |
| `open/high/low/close/volume` | 원본 그대로 |
| `first_seen_at` | 이 봉이 `_df_1m` 에 **처음** 나타난 시각 (KST ISO) ← **재현의 핵심** |
| `last_seen_at` | 마지막으로 값이 갱신된 시각 |
| `revision` | 값이 바뀐 횟수 (KIS 사후 정정 포착) |
| `source` | `bootstrap` \| `incremental` |
| `worker_instance_id` | 어느 프로세스가 봤는지 (재시작 경계 식별) |

**API**

```python
def record_frame(df_1m: pd.DataFrame, *, now: datetime, source: str,
                 worker_instance_id: str) -> dict: ...
    # 오늘 날짜분만 대상. 메모리 인덱스와 비교해 신규/변경분만 append.
    # 반환: {"new": n, "revised": n, "errors": n}

def load_frame_as_of(date_ymd: str, as_of: datetime) -> pd.DataFrame: ...
    # first_seen_at <= as_of 인 행만. 그 시점 worker 프레임의 정확한 복원.

def load_day(date_ymd: str) -> pd.DataFrame: ...
```

**성능**: 프로세스 내 dict 인덱스(`datetime -> (ohlcv, revision)`)를 유지해
매 사이클 diff 만 append 한다. 전체 rewrite 없음. 파일 append 는 `revision` 증가분
포함이므로 같은 `datetime` 이 여러 행 나올 수 있고, 읽기 시 `keep="last"` 로 접는다.

### 4-3. 훅 지점 — 기존 파일 변경 3곳 (전부 최소 침습)

**(a) `market_data.py` `merge_incremental_1m()` — `self._df_1m = merged` 직후**

```diff
             merged = _trim_to_recent_trading_days(merged)
             self._df_1m = merged
+            # 신호 재현 아카이브 (2026-09-08). 실패해도 절대 거래를 막지 않는다.
+            try:
+                bar_archive.record_frame(
+                    merged, now=now, source="incremental",
+                    worker_instance_id=self.worker_instance_id,
+                )
+            except Exception:
+                self._archive_error_count += 1
             return merged.copy()
```

**(b) `market_data.py` `bootstrap()` — 최종 `self._df_1m` 확정 직후**
동일 형태, `source="bootstrap"`.
bootstrap 은 당일 전체를 페이지 워크하므로 **그 시점 이전 봉들의 `first_seen_at`이
전부 bootstrap 시각으로 찍힌다** — 이는 사실 그대로다(worker 는 그 시각 이전에
그 봉들을 못 봤다). 이 성질이 오늘 같은 사고의 원인을 그대로 드러내 준다.

**(c) `service.py` / 대시보드** — 진단 표시용 1줄
`bar_archive.stats(today)` 로 `행수 / 최초관측 / revision 건수 / 쓰기실패 수` 노출.

### 4-4. ETF 1분봉 (범위 밖, 별도 결정 필요)

`market_data` 는 주석대로 **WATCH_SYMBOL(하이닉스) history 만** 추적하고 ETF 는 quote
스냅샷만 본다. 체결가 재현까지 하려면 ETF 1분봉도 같은 방식으로 남겨야 하지만,
그건 **새 KIS 호출을 추가**하는 일이라 rate-limit·지연 영향이 있다. 오늘 확인된 바로는
**ETF 사후조회 가격은 실제 체결과 일치**했으므로(§2 마지막 표), 우선순위는 낮다.
1단계에서는 하이닉스만 아카이브하고, ETF 는 사후조회를 계속 쓰되 이 결정을 문서에 남긴다.

---

## 5. 설계 ② — 3분 확정봉마다 MACD 기록

### 5-1. 기존 signal ledger 를 쓰지 않는 이유

- `ledger.append_signal()` 은 `signal_id` dedup 을 위해 **매 호출마다 CSV 전체를 스캔**
  한다(`ledger.py:295`). 봉마다 쓰면 O(n²) 로 장중 tick 을 물고 늘어진다.
- signal ledger 는 "주문 후보 1건의 생애 기록"이라는 의미가 있다. 크로스가 없는 봉까지
  넣으면 그 의미가 무너지고 기존 UI/집계가 전부 영향받는다.

→ **별도 원장 `macd2_bar_ledger.csv` 신설**(append-only, dedup 불필요).

### 5-2. 신규 파일 `app/trading/macd2/bar_ledger.py`

| 항목 | 값 |
|---|---|
| 경로 | `data_paths` 의 logs 디렉터리 / `macd2_bar_ledger.csv` (Persistent Disk) |
| 쓰기 | append-only, dedup 없음. 같은 `bar_at` 이 재시작으로 중복되면 읽기 시 `keep="last"` |
| 크기 | 1일 ~130행 × ~250B ≈ **33KB/일** |
| 보호 | `ledger._assert_safe_to_write_ledger()` 와 **동일한 가드 재사용**(ad-hoc 스크립트가 실서버 원장에 쓰는 2026-08-19 사고 재발 방지) |

**스키마 — 요청하신 5개(macd/signal/gap/direction/confirmed_cross) + 재현에 필요한 최소 컨텍스트**

| 컬럼 | 설명 |
|---|---|
| `trading_date`, `bar_at`, `bar_end_at`, `observed_at` | 봉 시각 / 판정 시각 |
| `open,high,low,close,volume` | 그 3분봉 |
| **`macd`, `signal`, `gap`** | `gap = macd − signal` (요청) |
| `hist_last3`, `prev_gap` | 크로스 판정 입력 그대로 |
| **`direction`** | `evaluate_macd_crossover()` 반환 (`UP_RED`/`DOWN_BLUE`/`HOLD`) (요청) |
| **`confirmed_cross`** | `direction in (UP_RED, DOWN_BLUE)` (요청) |
| `prev_direction_state` | 그 시점 `state.last_detected_direction` — **크로스 억제 로직 재현에 필수** |
| `bars_in_frame`, `oldest_bar_at` | 프레임 크기/시작 — **오늘 사고의 직접 원인 지표** |
| `premarket_bars_in_frame` | 당일 09:00 이전 완성 3분봉 수 — **핵심 진단값** |
| `dropped_incomplete_bars` | `filter_complete_3m_bars` 가 드롭한 수 |
| `signal_id` | 크로스일 때 signal ledger 행과 조인 (없으면 공란) |
| `worker_instance_id`, `worker_code_sha`, `strategy_name`, `strategy_version` | 출처 |

### 5-3. 훅 지점 — `worker.py` 변경 1곳

`run_once()` 에서 `bars_3m` 이 확정된 직후(현재 4384~4392행 부근),
**새 확정봉이 생긴 tick 에서만** 1회:

```diff
     bars_3m, _history_gap_bar_starts = filter_complete_3m_bars(bars_3m, df_1m)
+    # 3분 확정봉 MACD 원장 (2026-09-08). 크로스 유무와 무관하게 전 봉 기록.
+    # 쓰기 실패는 거래를 막지 않는다.
+    try:
+        bar_ledger.record_bar(
+            state=state, bars_3m=bars_3m, df_1m=df_1m, now=now,
+            dropped=_history_gap_bar_starts,
+        )
+    except Exception:
+        pass
```

`record_bar` 내부에서 `state.bar_ledger_last_bar_at` 과 비교해 **같은 봉 중복기록을
차단**한다. MACD 는 `calculate_macd(bars_3m)` 로 이미 계산되는 값을 재사용하는 것이
이상적이지만, 현재 그 호출은 이 지점보다 뒤에 있다 → 두 가지 선택지:

- **(권장) 계산 1회 앞당겨 공유**: `macd_snap = calculate_macd(bars_3m)` 를 이 지점으로
  올리고 기존 호출부에 전달. 순수함수라 결과 불변, 연산 1회 절감.
- (대안) `record_bar` 안에서 한 번 더 호출. 코드 이동 없음 대신 CPU 중복.

### 5-4. 상태 필드 추가 — `state_store.py`

```diff
+    bar_ledger_last_bar_at: Optional[str] = None   # 중복기록 차단
```

---

## 6. 변경점 요약 (아직 적용하지 않음)

| 파일 | 종류 | 내용 | 위험도 |
|---|---|---|---|
| `app/trading/macd2/bar_archive.py` | **신규** | 1분봉 프레임 아카이브 (~150줄) | 낮음 (독립 모듈) |
| `app/trading/macd2/bar_ledger.py` | **신규** | 3분 확정봉 MACD 원장 (~120줄) | 낮음 (독립 모듈) |
| `app/trading/macd2/market_data.py` | 수정 | `merge_incremental_1m` / `bootstrap` 에 훅 각 1곳 (try/except 감쌈) | 낮음 |
| `app/trading/macd2/worker.py` | 수정 | `run_once` 에 훅 1곳 (+ `calculate_macd` 호출 위치 조정 권장) | **중** — 거래 경로 |
| `app/trading/macd2/state_store.py` | 수정 | 필드 1개 추가 | 낮음 |
| `app/trading/macd2/service.py` | 수정 | 대시보드 진단 1줄 | 낮음 |
| `tests/macd2/` | 신규 | 아카이브 round-trip / `load_frame_as_of` 시점복원 / 원장 스키마 / 쓰기실패시 거래 무영향 | — |

**설계 불변식**
1. 아카이브·원장 쓰기 실패는 **절대** 주문/청산 경로를 막지 않는다(전부 try/except + 카운터).
2. 두 파일 모두 **Persistent Disk(`AI_GAP_DATA_DIR`) 하위**여야 한다 — 아니면 재배포 때 소실.
3. 기존 `signal ledger` / `execution ledger` 의 스키마·의미는 **건드리지 않는다**.
4. `_assert_safe_to_write_ledger()` 가드를 신규 원장에도 그대로 적용한다.

---

## 7. 검증 계획 (구현 후)

1. **1일 수집** — Render 배포 후 하루치 `bar_archive` + `bar_ledger` 확보.
2. **재현율 재측정** — `load_frame_as_of(date, 각 T+3 시각)` 으로 프레임을 복원해
   백테스트 엔진에 넣고, 그날 signal ledger 의 플래그·T+3 을 **7/7 재현하는지** 확인.
   목표 **100%**. 미달이면 `bar_ledger` 의 `macd/signal/gap` 과 직접 대조해 어느
   단계에서 갈리는지 특정한다(프레임 / MACD / 게이트 중 하나로 좁혀진다).
3. **A/B/C 재비교** — 같은 날짜에 대해 A(아카이브) vs B(사후조회) vs C(정규장만)
   재현율을 다시 낸다. A 가 100%, B 가 40%대이면 설계가 검증된 것이다.
4. 그 **다음에** TW2 / TWF / B(CHOP 재게이트) 수익률 연구를 아카이브 입력으로 재실행한다.

---

## 8. 파일 / 재현

```
scratchpad/run40_repro.py    # §2 재현율 비교 전체
```

| 파일 | 내용 |
|---|---|
| `README.md` | 본 문서 |
