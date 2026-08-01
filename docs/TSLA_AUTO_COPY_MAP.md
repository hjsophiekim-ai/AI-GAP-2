# TSLA_AUTO Copy Map

Current US market-session policy: every TSLA_AUTO path must use
`app.trading.tsla_auto.market_session.get_us_market_state()` and the shared
`USMarketSessionState`. Any fixed 15:40/15:45/15:50 ET or fixed KST time text
below is historical copy-analysis context only and is superseded by actual
exchange `session_close_et - 15 minutes` entry blocking and
`session_close_et - 10 minutes` forced liquidation.

MACD2(`app/trading/macd2/`) 파일별 재사용 가능성 분석. 각 파일을 실제로 읽고 호출 관계를
확인한 뒤 분류했다(`docs/TSLA_AUTO_REQUIREMENTS.md` §0 근거 자료 참조).

분류 정의:

- **COPY_AS_IS**: 로직 변경 없이 그대로 복제(파일명/모듈 경로만 바뀜)
- **COPY_AND_RENAME**: 로직은 동일하지만 이름(심볼/상수 등)만 바꿔 복제
- **COPY_WITH_US_MARKET_CHANGE**: 구조는 유지하되 시간대·세션·통화 등 미국시장 요소를 바꿔야 함
- **REWRITE_FOR_KIS_OVERSEAS**: KIS 해외주식 API 호출부라서 사실상 새로 작성해야 함
- **DO_NOT_COPY**: 재사용하지 않음(공유 금지 대상이거나 국내 전용이라 무관)

각 파일은 아래 9개 항목으로 정리한다: MACD2 원본 파일 / TSLA_AUTO 예정 파일 / 분류 / 유지 기능
/ 미국시장 변경점 / KIS 해외 API 변경점 / MACD2와 분리방법 / 위험 / 필수 테스트.

---

## 1. `config.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/config.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/config.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** |
| 유지 기능 | `EMA_FAST/SLOW/SIGNAL`, `STOP_LOSS_NET_PCT`, `PROFIT_LOCK_ACTIVATE_NET_PCT`, `PROFIT_LOCK_GIVEBACK_PP`, `MAJOR_*` Hybrid 필터 상수 전체(§`TSLA_AUTO_LOGIC.md` 표), `QUOTE_MAX_AGE_SEC`, `WORKER_INTERVAL_SEC`, ledger/state 파일명 상수 패턴 |
| 미국시장 변경점 | `KST`→`America/New_York`(zoneinfo), `SESSION_OPEN(09:00)`→`09:30`, `NEW_ENTRY_CUTOFF(14:55)`→`15:40`(+신규 `LATE_NEW_BUY_CUTOFF_ET=15:45`), `FORCE_LIQUIDATE_AT(15:00)`→`15:50`+최종확인 `15:58`. `TRADE_SYMBOLS=(LONG_SYMBOL, INVERSE_SYMBOL)`→`(TSLL, TSLZ)`, `WATCH_SYMBOL="000660"`→`"TSLA"`. 신규 상수: `STOP_LOSS_REENTRY_COOLDOWN_MIN=15`, `STOP_LOSS_REENTRY_OVERRIDE_SCORE_MIN`(=`max(85, 문턱)` 계산용 베이스), `DEFAULT_BUDGET`→USD 단위, `TSLA_AUTO_ORDER_USAGE_RATIO` |
| KIS 해외 API 변경점 | 없음(순수 상수 파일) |
| MACD2와 분리방법 | 별도 모듈 경로, `STRATEGY_NAME="TSLA_AUTO"`, 별도 `RUNTIME_STATE_FILENAME`/`SIGNAL_LEDGER_FILENAME`/`EXECUTION_LEDGER_FILENAME` |
| 위험 | 시간 상수를 KST로 잘못 남겨두면 세션 판정 전체가 틀어짐 — 상수 하나하나 시간대 명시 필요 |
| 필수 테스트 | 모든 시간 상수가 `America/New_York` tz-aware인지, `TSLL`/`TSLZ`가 정확히 설정됐는지 단위 검증 |

## 2. `models.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/models.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/models.py` |
| 분류 | **COPY_AND_RENAME** |
| 유지 기능 | `Direction`, `SignalState`, `RuntimeStatus` enum 구조, `MacdSnapshot`/`QuoteSnapshot`/`PositionSnapshot`/`MajorFlagDecision` dataclass 구조, `RuntimeState`의 필드 패턴(강한 필터·candidate·주문 사이징 진단 필드 전체) |
| 미국시장 변경점 | `_require_tz_aware`의 docstring/에러 메시지가 "KST"라고 고정 서술 — "tz-aware(ET)"로 문구 변경. `RuntimeState`에 신규 필드 추가: `last_stop_loss_exit_at`, `stop_loss_cooldown_direction`, `stop_loss_reentry_override_used_today`, ET/KST 이중 표시용 필드(`*_at_et`/`*_at_kst`) |
| KIS 해외 API 변경점 | 없음(순수 타입 정의) |
| MACD2와 분리방법 | 별도 모듈 — import 공유 없음(같은 구조를 복제) |
| 위험 | 필드명이 같아 실수로 MACD2 모듈을 import해버릴 위험(§분리 테스트로 가드) |
| 필수 테스트 | dataclass 생성/직렬화 왕복, tz-naive datetime 거부 |

## 3. `signal_engine.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/signal_engine.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/signal_engine.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE**(계산식은 COPY_AS_IS, 세션 경계 판정만 변경) |
| 유지 기능 | `resample_completed_3m`(`label="left", closed="left"`), `calculate_macd`(EMA 12/26/9, `adjust=False`), `evaluate_macd_crossover`, `make_signal_id` 패턴, `forming_bar_window`, `is_tradeable_completed_bar` — 함수 시그니처와 알고리즘 100% 동일하게 유지 가능(입력이 tz-aware datetime이면 시간대에 무관하게 동작하는 순수 함수이기 때문) |
| 미국시장 변경점 | `is_tradeable_completed_bar`/`forming_bar_window` 등에서 `config.SESSION_OPEN` 비교가 `America/New_York` 09:30 기준이 되도록 config 참조만 교체(함수 본문 로직 변경 없음) |
| KIS 해외 API 변경점 | 없음(순수 함수, 네트워크 없음) |
| MACD2와 분리방법 | 별도 모듈로 전체 복제(import 아님) — 동일 수식을 두 곳에 유지 |
| 위험 | 가장 손대면 안 되는 파일 — MACD2 쪽 원본은 절대 수정하지 않는다(어떤 TSLA_AUTO 작업도 `app/trading/macd2/signal_engine.py` diff를 만들면 안 됨) |
| 필수 테스트 | MACD2 `tests/macd2/test_signal_engine.py`와 대칭되는 09:30 기준 버전 전체 이식, DST 경계일 리샘플 테스트 신규 추가 |

## 4. `major_flag_filter.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/major_flag_filter.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/major_flag_filter.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** |
| 유지 기능 | `_atr`/`_true_range`(Wilder), `_macd_lines`, `compute_component_scores`/`score_for_direction`의 A~G 배점·문턱값 전체(§`TSLA_AUTO_LOGIC.md` 표), `evaluate_major_flag`/`apply_major_trade_gates`의 게이트 순서(사이드웨이즈 → 가격확인 → 점수 → 동일방향보유 → 일일한도 → 재진입쿨다운 → 최소보유) |
| 미국시장 변경점 | **`_session_vwap()`이 `work["datetime"].dt.tz_convert(config.KST)`와 `config.SESSION_OPEN`/`FORCE_LIQUIDATE_AT`(한국 정규장 시각)에 하드코딩되어 있음 — America/New_York 09:30~16:00 세션 기준으로 재작성 필요.** 이 함수 하나만 재작성 대상이고 나머지 지표는 OHLCV/ATR 기반이라 시간대 무관 |
| KIS 해외 API 변경점 | 없음(순수 함수) |
| MACD2와 분리방법 | 별도 모듈 전체 복제 |
| 위험 | VWAP 재작성을 빠뜨리면 세션 경계가 한국 시각으로 계산되어 F(EMA20/VWAP) 항목 점수가 조용히 틀어짐(가장 놓치기 쉬운 버그 포인트) |
| 필수 테스트 | `tests/macd2/test_major_flag_filter.py` 전체를 09:30 ET 기준으로 이식 + VWAP 세션 경계 전용 신규 테스트(자정 넘어가는 경우 없음 확인, DST 경계일) |

## 5. `market_data.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/market_data.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/market_data.py` |
| 분류 | **REWRITE_FOR_KIS_OVERSEAS**(구조는 유지, 네트워크 호출부는 전면 재작성) |
| 유지 기능 | `MarketDataService`의 전체 구조(bootstrap → incremental merge → quote cache), 페이지 병합/dedup/sort 패턴, `filter_complete_3m_bars`(2026-07-31 신규 게이트, §`TSLA_AUTO_LOGIC.md` 데이터), 히스토리/쿼트 업데이터 스레드 구조, `quote_status`/`quote_statuses` 진단 |
| 미국시장 변경점 | 페이지 크기(`KIS_PAGE_SIZE`)·페이징 한도(`KIS_MAX_PAGES`)는 해외분봉조회 응답 크기(최대 120건/회, §KIS 해외 API)에 맞게 재산정. 전일 거래일 탐색은 미국 캘린더 기준 |
| KIS 해외 API 변경점 | `_default_fetch_minute_candles`/`_default_fetch_minute_candles_for_date`/`_default_fetch_quote`를 `app/data_sources/kis_overseas_minute.py`의 `HHDFS76950200`/`HHDFS00000300` 호출로 전면 교체. **1분봉 요청 1회당 최대 120건**이라는 제약이 MACD2의 "1회 약 30건" 가정과 달라 페이징 루프 상수를 다시 계산해야 함 |
| MACD2와 분리방법 | 별도 모듈, 별도 `CACHE_DIR/tsla_auto/`(§`TSLA_AUTO_LOGIC.md` 원장/분리 참고사항) |
| 위험 | 현재가상세(`HHDFS00000300`)·분봉조회(`HHDFS76950200`)의 실제 field 이름은 확인됐지만, 페이지네이션 커서(`NEXT`/`KEYB`) 동작 방식이 MACD2의 `hour1` 백워드 커서와 다를 수 있음 — `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED` |
| 필수 테스트 | fake fetcher로 warm-up/incremental merge/gap 필터 전체 이식, 페이지 120건 경계 테스트 |

## 6. `broker_adapter.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/broker_adapter.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/broker_adapter.py` |
| 분류 | **REWRITE_FOR_KIS_OVERSEAS** |
| 유지 기능 | `BrokerOrderResult`/`BuySizingQuote` dataclass 형태, `MockBrokerAdapter`/`RealBrokerAdapter` 이원화 구조, `create_macd2_broker`류 팩토리 패턴, REAL 게이트가 브로커 생성 시점에 걸리는 설계(`docs/MACD2_LOGIC.md` §14와 동일 원칙 유지) |
| 미국시장 변경점 | 없음(통화/시간 로직은 이 계층에 없음) |
| KIS 해외 API 변경점 | **이 파일이 감싸는 `app.trading.broker_factory.create_broker`/`BrokerBase`는 국내(KRX) 전용이라 그대로 재사용 불가.** 해외주식 잔고/매수가능금액/주문/취소 TR을 호출하는 새 하위 계층이 필요하며, 그 TR 세부사항은 전부 `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`(§`TSLA_AUTO_LOGIC.md` KIS 해외주식 API) |
| MACD2와 분리방법 | 별도 모듈, 국내 `broker_factory`/`broker_base`를 import하지 않음(대신 신규 해외전용 브로커 클래스) |
| 위험 | **가장 큰 위험 지점** — 해외 주문 TR이 이 저장소에 선례가 없어 order/balance 관련 필드명·부호·통화 단위를 잘못 가정할 가능성이 높음 |
| 필수 테스트 | fake broker(테스트 더블)로 MACD2와 동일한 안전성 테스트 이식 + REAL 게이트 우회 불가 테스트 |

## 7. `order_executor.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/order_executor.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/order_executor.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** |
| 유지 기능 | `execute_signal`/`execute_exit`의 전체 흐름(반대신호 SELL→reconcile→BUY, 주문 접수≠체결 확인, 체결 폴링, 부분체결/취소, `signal_id` 중복 차단, `ExecutionOutcome`의 풍부한 진단 필드), `compute_limit_buy_quantity`류 사이징 함수의 구조(`min(예산, 실제가능금액) × usage_ratio` 후 1주 단위 재검증) |
| 미국시장 변경점 | 통화 단위 KRW→USD, tick size(호가단위) 계산이 KRX `get_tick_size` 대신 미국 ETF의 실제 tick size 규칙 필요(§`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`) |
| KIS 해외 API 변경점 | `buy_limit`/`get_buy_sizing_quote`/`get_fresh_ask1`가 호출하는 하위 브로커 메서드가 해외주식 TR로 교체됨(§broker_adapter.py) |
| MACD2와 분리방법 | 별도 모듈, `TradeCostEngine` 재사용 시 해외 수수료 설정 섹션을 신규로 참조 |
| 위험 | KRW 정수 원 단위 가정이 코드 곳곳에 있을 수 있음 — USD는 소수점(센트) 단위이므로 반올림/정수화 로직 전체 재검토 필요 |
| 필수 테스트 | MACD2 `tests/macd2/test_order_executor.py` 전체를 USD 사이징으로 이식, 센트 단위 반올림 테스트 신규 |

## 8. `worker.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/worker.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/worker.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** + 신규 로직 추가 |
| 유지 기능 | `run_once` tick 구조, `_advance_confirmed_primary`(봉-once 게이트), `_dispatch_confirmed_signal`/`_execute_or_wait`(quote stale 동기 재조회 포함), `reconcile_position_state`, `_apply_switch_outcome`/`_apply_exit_outcome`, `_record_signal_ledger`, `Macd2Worker` 단일 스레드 구조, `filter_complete_3m_bars` 게이트(2026-07-31), `compute_today_signal_overview`(LIVE_CONFIRMED/HISTORICAL_REPLAY_ONLY) |
| 미국시장 변경점 | 모든 `now`/`SESSION_OPEN`/`NEW_ENTRY_CUTOFF`/`FORCE_LIQUIDATE_AT` 비교를 ET 기준으로. **(신규)** 15:45 ET 이후 신규진입·반대매수 전부 차단 게이트 추가(§`TSLA_AUTO_LOGIC.md` 손절·Profit Lock·전환) |
| KIS 해외 API 변경점 | 없음(이 파일 자체는 broker/market_data를 통해서만 KIS와 접촉 — 직접 호출 없음, MACD2와 동일 원칙 유지) |
| MACD2와 분리방법 | 별도 모듈, 별도 lock file(`tsla_auto_worker.lock`) |
| 위험 | **(신규) 손절 재진입 쿨다운 + 하루 1회 85점 예외**는 MACD2에 없는 완전히 새로운 상태 머신이라 회귀 위험이 가장 큼 — 이 부분만 별도로 충분한 테스트가 필요. 또한 MACD2가 2026-07-31에 겪은 "진행봉 candidate가 실제 주문 권한을 갖는" 회귀를 처음부터 재현하지 않도록 코드 리뷰 단계에서 candidate 경로가 broker를 호출하지 않는지 반드시 확인 |
| 필수 테스트 | MACD2 `tests/macd2/test_worker.py`/`test_execution_gates.py` 전체 이식 + 신규 쿨다운/85점 예외/15:45 컷오프 전용 테스트 |

## 9. `service.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/service.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/service.py` |
| 분류 | **COPY_AND_RENAME** |
| 유지 기능 | `Macd2Service`의 생명주기(quote-cache-ready → bootstrap → Worker start 순서), `get_snapshot()`/`supervisor_status()` 구조, 프로세스 싱글턴 패턴(`get_service()`) |
| 미국시장 변경점 | 없음(시간 로직은 하위 모듈에 위임) |
| KIS 해외 API 변경점 | 없음(이 파일도 직접 KIS를 호출하지 않음) |
| MACD2와 분리방법 | 별도 싱글턴(`_service_instance`), `other_strategy_active()`류 상호배제 체크는 **TSLA_AUTO에는 이식하지 않음**(§`TSLA_AUTO_LOGIC.md` MACD2와 완전 분리 — `strategy_ownership.py` 국내 3파전에 참여하지 않으므로) |
| 위험 | 실수로 `app.trading.strategy_ownership`을 import해 국내 전략과 얽히는 것 — 안 하는 게 맞으므로 애초에 import하지 않도록 설계 |
| 필수 테스트 | 싱글턴 재사용/재설정, MACD2 서비스 인스턴스와 상태 공유 없음 확인 |

## 10. `state_store.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/state_store.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/state_store.py` |
| 분류 | **COPY_AS_IS**(직렬화 구조), 경로만 변경 |
| 유지 기능 | `default_state`/`serialize`/`deserialize`/`load_state`/`save_state`의 원자적 쓰기 패턴(tmp write + replace 추정 — MACD2와 동일하게 유지) |
| 미국시장 변경점 | 없음(직렬화 로직 자체는 시간대 무관 — 문자열 ISO 저장) |
| KIS 해외 API 변경점 | 없음 |
| MACD2와 분리방법 | `STATE_DIR_PATH`/`STATE_PATH`를 `data/state/tsla_auto/tsla_auto_runtime.json`으로 분리 |
| 위험 | 낮음 — 순수 직렬화 계층 |
| 필수 테스트 | 신규 필드(§models.py) 포함 왕복 직렬화 테스트 |

## 11. `ledger.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/ledger.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/ledger.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** |
| 유지 기능 | `_append_row`의 디스크 헤더 재정렬 안전장치, `append_signal`/`append_execution`의 `signal_id`/`order_id` dedup, `_current_strategy_rows`(strategy_version/signal_rule/worker_code_sha/세션 필터, 2026-07-31 수정 포함), `summarize_signals`/`summarize_daily_trading` |
| 미국시장 변경점 | 없음(시간 문자열은 그대로 저장 — ET/KST 이중 컬럼만 추가) |
| KIS 해외 API 변경점 | 없음 |
| MACD2와 분리방법 | 별도 `SIGNAL_LEDGER_FILENAME`/`EXECUTION_LEDGER_FILENAME`, 별도 경로(`data/ledger/tsla_auto/` — §`TSLA_AUTO_LOGIC.md` 분리 절의 경로 관례 불일치 메모 참조) |
| 위험 | 컬럼 목록에 ET/KST 이중 필드·USD 사이징 필드·손절 쿨다운 필드를 추가하면서 기존 컬럼 순서를 흔들지 않아야 함(MACD2가 2026-07-27에 겪은 "열 밀림" 사고 재발 방지) |
| 필수 테스트 | `tests/macd2/test_ledger.py` 전체 이식 + 컬럼 순서/헤더 재정렬 테스트 |

## 12. `risk_exit.py`

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/macd2/risk_exit.py` |
| TSLA_AUTO 예정 파일 | `app/trading/tsla_auto/risk_exit.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** + 신규 로직 추가 |
| 유지 기능 | `check_stop_loss`/`update_profit_lock_tracker`/`evaluate_position_exits`의 순수 함수 구조와 우선순위(손절 > Profit Lock) |
| 미국시장 변경점 | 없음(비율 계산 자체는 통화 무관) |
| KIS 해외 API 변경점 | 없음(순수 함수, net_return_pct는 상위에서 계산해 전달받음) |
| MACD2와 분리방법 | 별도 모듈 |
| 위험 | **(신규)** 손절 후 15분 쿨다운 + 하루 1회 85점 예외 로직을 이 파일에 추가할지 `worker.py`에 둘지 설계 결정 필요(현재 설계안은 `worker.py`의 게이트로 배치 — §`TSLA_AUTO_LOGIC.md`) — 이 파일 자체의 손절/Profit Lock 판정 함수는 변경하지 않는다 |
| 필수 테스트 | `tests/macd2/test_risk_exit.py` 그대로 이식(로직 변경 없음이므로 값만 확인) |

## 13. `cost_engine`(`app/trading/trading_cost_engine.py` — MACD2 전용 파일이 아니라 공용 모듈)

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/trading_cost_engine.py`(MACD2 전용이 아닌 국내 공용 비용 엔진 — `order_executor.py`가 import) |
| TSLA_AUTO 예정 파일 | 신규 `app/trading/tsla_auto/overseas_cost_engine.py`(가칭) |
| 분류 | **REWRITE_FOR_KIS_OVERSEAS**(패턴은 재사용, 값은 전부 새로 정의) |
| 유지 기능 | `config.yaml`에서 요율을 읽어오는 패턴(`get_config().trading_cost`), 매수/매도 수수료를 대칭적으로 다루는 `compute_trade_cost`/`compute_net_pnl` 구조 |
| 미국시장 변경점 | 통화 USD, 수수료 항목 자체가 다름(SEC Section 31 fee, FINRA TAF 등 미국 규제 수수료가 추가로 필요 — 국내 ETF 수수료 체계에는 없는 개념) |
| KIS 해외 API 변경점 | `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: KIS 실제 해외주식 수수료율(및 최소수수료·환전수수료율) 공식 확인 전까지 값 고정 금지 |
| MACD2와 분리방법 | 기존 `TradeCostEngine`을 수정하지 않고 새 클래스/모듈로 분리(국내 로직에 영향 0) |
| 위험 | 수수료율을 잘못 가정하면 Net PnL이 체계적으로 왜곡됨 — 실제 KIS 체결내역 비용이 있으면 그 값을 항상 추정치보다 우선 사용하도록 설계(§`TSLA_AUTO_LOGIC.md` 비용·손익) |
| 필수 테스트 | Gross/Net 계산 단위테스트, 실제 체결내역 비용 우선 적용 테스트 |

## 14. UI (`app/ui/pages/11_MACD_자동매매2.py`)

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/ui/pages/11_MACD_자동매매2.py` |
| TSLA_AUTO 예정 파일 | `app/ui/pages/`아래 신규 페이지(가칭 `12_TSLA_자동매매.py`) |
| 분류 | **COPY_WITH_US_MARKET_CHANGE** |
| 유지 기능 | "command 기록 + snapshot 표시만" 원칙, 패널별 독립 `try/except`(한 패널 오류가 나머지를 막지 않음), MAJOR 필터 토글/통계 UI 구조, `_signal_display_time`류 시간 포맷 헬퍼 패턴 |
| 미국시장 변경점 | 모든 시간 표시를 ET·KST 동시 표기로 변경. 예산 입력이 USD 단위(원화 콤마 포맷 대신 USD 포맷) |
| KIS 해외 API 변경점 | 계좌 마스킹/잔고 표시가 해외 잔고 조회 응답 스키마에 맞게 조정(§`TSLA_AUTO_LOGIC.md` KIS 해외주식 API — 확정 전까지는 필드 매핑 보류) |
| MACD2와 분리방법 | 별도 페이지 파일, `app.trading.tsla_auto.service.get_service()`만 호출 — MACD2 서비스/모듈 import 없음 |
| 위험 | 두 페이지가 같은 Streamlit 프로세스에서 동시에 열릴 때 세션 상태 키 충돌(예: `st.session_state["mode"]`류 공용 키 사용 금지 — 페이지별 prefix 필요) |
| 필수 테스트 | `tests/macd2/test_ui_page.py`와 대칭되는 `AppTest` 렌더 테스트, ET/KST 동시 표시 검증 |

## 15. KIS 인증·API 연결

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `app/trading/kis_client.py`(국내 전용), `app/config.py`(`get_kis_account_config`), `app/data_sources/kis_overseas_minute.py`(해외 시세/분봉, MU 전용으로 이미 운영 중) |
| TSLA_AUTO 예정 파일 | 신규 `app/trading/tsla_auto/kis_overseas_client.py`(가칭) — `kis_overseas_minute.py`의 인증/시세 로직을 일반화해 TSLA/TSLL/TSLZ에 재사용하고, 해외 주문/잔고 메서드를 추가 |
| 분류 | 인증/토큰 계층은 **COPY_AS_IS**(계좌 설정 재사용) / 시세·분봉은 **COPY_AND_RENAME**(MU→TSLA 일반화) / 주문·잔고는 **REWRITE_FOR_KIS_OVERSEAS**(선례 없음) |
| 유지 기능 | `get_kis_account_config(mode)`의 앱키/계좌번호 우선순위 로직 그대로 재사용(국내·해외가 같은 자격증명 사용 — 실제 확인됨). `kis_overseas_minute.py`의 토큰 캐시(파일 캐시 + credential fingerprint 검증), rate-limit 회피용 페이싱 패턴 |
| 미국시장 변경점 | 없음(인증은 시장 무관) |
| KIS 해외 API 변경점 | 시세(`HHDFS00000300`)·분봉(`HHDFS76950200`)은 재사용. 주문/잔고/주문가능금액·수량/정정취소/미체결/체결내역은 전부 `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`(§`TSLA_AUTO_LOGIC.md` 표 — 이 저장소에 선례 없음). **경로 불일치 발견**: `kis_overseas_minute.py`는 `/uapi/overseas-price/...`, `auto_market_collector.py`는 같은 TR로 `/uapi/overseas-stock/...`를 사용 — 공식 확인 필요 |
| MACD2와 분리방법 | `kis_client.py`(국내 주문 함수)를 TSLA_AUTO가 import하지 않음(테스트로 가드). 인증 계층만 공유하고 도메인 로직은 완전히 분리 |
| 위험 | 해외 주문/잔고 TR 전체가 이 저장소에 선례가 없다는 것이 **가장 큰 구현 리스크**(§`TSLA_AUTO_LOGIC.md` KIS 해외 API 표) |
| 필수 테스트 | 국내 주문 함수 호출 0건 가드, 인증 자격증명 공유 확인(같은 env var를 읽는지), MOCK 모드에서 해외 주문 TR이 실제로 지원되는지 사전 확인용 스모크 테스트 |

## 16. `tests/macd2` 전체

| 항목 | 내용 |
|---|---|
| MACD2 원본 파일 | `tests/macd2/*.py`(conftest 포함, 약 20개 파일) |
| TSLA_AUTO 예정 파일 | `tests/tsla_auto/*.py` |
| 분류 | **COPY_WITH_US_MARKET_CHANGE**(테스트 자체는 대부분 그대로 이식 가능, 시각/종목명만 치환) |
| 유지 기능 | `conftest.py`의 autouse 격리 fixture 패턴(state/ledger/cache tmp_path 리다이렉트, 실제 네트워크/실제 KIS client 차단), `FakeBroker` 테스트 더블 패턴, 시나리오 생성 헬퍼(`_1m_from_3m_closes`류) |
| 미국시장 변경점 | 모든 `datetime(..., tzinfo=KST)`를 `America/New_York`으로, 세션 시각(09:00→09:30 등)을 전부 치환 |
| KIS 해외 API 변경점 | 해외 fetcher를 흉내내는 fake 함수로 교체(실제 KIS 해외 TR 호출 절대 금지) |
| MACD2와 분리방법 | 별도 디렉터리 `tests/tsla_auto/`, 별도 `conftest.py`(TSLA_AUTO 경로만 격리) |
| 위험 | 테스트를 기계적으로 치환하다 시간대 관련 assert(예: `"HH:MM:SS"` 하드코딩)를 놓치면 통과하지만 실제로는 틀린 테스트가 될 수 있음 |
| 필수 테스트 | (이 항목 자체가 테스트 이식 대상이므로) 이식 후 `python -m pytest tests/tsla_auto -q` 전체 통과 + MACD2 `tests/macd2` 결과 불변 확인 |

---

## 요약 — 분류별 파일 수

| 분류 | 파일 수 |
|---|---|
| COPY_AS_IS | 2 (`state_store.py`의 직렬화 구조, KIS 인증/토큰 계층) |
| COPY_AND_RENAME | 3 (`models.py`, `service.py`, KIS 시세/분봉 계층) |
| COPY_WITH_US_MARKET_CHANGE | 8 (`config.py`, `signal_engine.py`, `major_flag_filter.py`, `order_executor.py`, `worker.py`, `risk_exit.py`, `ledger.py`, UI, tests) |
| REWRITE_FOR_KIS_OVERSEAS | 4 (`market_data.py`, `broker_adapter.py`, cost engine, KIS 해외 주문/잔고 계층) |
| DO_NOT_COPY | 국내 전용 파일 전부(`kis_client.py`의 주문 함수, `strategy_ownership.py`, `broker_factory.py`/`broker_base.py`의 국내 구현체) — TSLA_AUTO에서 import하지 않음 |

(개수는 "파일" 단위가 아니라 위 16개 분석 항목 기준 — 일부 항목은 하나의 파일 안에서 계층별로
분류가 갈린다, 예: KIS 인증·API 연결.)
# TSLA_AUTO Copy Map Addendum: US Market Session

All copied US-market timing rules are superseded by
`app.trading.tsla_auto.market_session.USMarketSessionState` and
`docs/US_MARKET_SESSION_POLICY.md`. Do not copy fixed 15:45/15:50 ET or fixed
KST session windows into worker, UI, order executor, or signal code. Entry
cutoff and forced liquidation are always computed from the actual exchange
calendar close.
