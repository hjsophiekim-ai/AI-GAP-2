# ws_shadow_logger — 독립 KIS WebSocket SHADOW 로거 (READ-ONLY)

MACD2 ADAPTIVE-EXIT 연구(회복 vs 붕괴 / 지속 vs 반납 / 진짜 반전 vs 휩쏘)에 쓸 실시간 미시구조 데이터를 모은다.
production(`app/*`)과 코드·상태파일·키를 완전히 분리했다.
- 주문 함수와 REST 토큰 발급 경로는 import도 하지 않는다(`tests/test_isolation.py`가 소스 전체를 검사한다).
- 자동매매 ON/OFF, 신규진입 허용, 전략모드(N1/P3/E/CHOP), UI 세션 상태를 읽지 않는다(`tests/test_service.py`).
- `app/` 코드는 이 패키지를 import 하지 않는다.

## Render 에서 상시 실행 (2026-10-15)

Start Command: `bash scripts/render_start_with_logger.sh` — 기존 streamlit 명령을 foreground 로 그대로 돌리고,
`KIS_LOGGER_ENABLED=1` 이면 로거를 별도 감독 프로세스로 붙인다. 자세한 동작은 `docs/deploy_render.md`.

실행 조건은 `KIS_LOGGER_ENABLED=1` 과 시간뿐이다.
- 프로세스는 계속 살아 있다. 평일 08:00~15:30 KST 에만 접속·구독·저장한다.
- 장외에는 연결 없이 idle 한다. 15:30 이후 maintenance(raw gzip + 일별 요약)를 한 번 돌린다. 다음 날 08:00 에 자동 재개한다.
- 끊기면 지수 백오프(최대 60초)로 재접속한다. 접속키는 로거 전용 key 로만 발급하고 하루 캐시한다.
- 1분마다 `[ws_shadow_logger] HEALTH LOGGER_RUNNING=1 WS_CONNECTED= LAST_MESSAGE_AT= DATA_DIR= TODAY_RAW_COUNT= TODAY_SNAPSHOT_COUNT= RECONNECT_COUNT= ... RSS_MB= MAX_RSS_MB=` 를 찍는다.

| 환경변수 | 설명 |
|---|---|
| `KIS_LOGGER_ENABLED` | `1` 일 때만 실행 |
| `KIS_LOGGER_APP_KEY` / `KIS_LOGGER_APP_SECRET` | 로거 전용 실전 appkey. `KIS_APP_KEY` / `KIS_REAL_APP_KEY` / `KIS_MOCK_APP_KEY` 와 같으면 거부(appkey 지문만 비교, production secret 은 읽지 않음) |
| `KIS_LOGGER_DATA_DIR` | 필수. 절대경로. 소스 디렉토리·임시 디렉토리는 거부. Render 에서는 영구 디스크 마운트 아래여야 한다 |
| `KIS_LOGGER_MAX_RSS_MB` | 선택. 기본 = 컨테이너(cgroup) 메모리 한도의 18%, 못 읽으면 96MB |

종료 코드: 0 정상/비활성 · 2 키 문제 · 3 데이터 경로 문제(쓰기 불가 포함) · 4 RSS 한도 초과. 래퍼는 이 넷은 재시작하지 않고, 그 외 비정상 종료만 백오프 재시작한다. 어떤 경우에도 UI/자동매매 프로세스는 건드리지 않는다.

## 메모리
- raw 프레임은 받는 즉시 파일에 append 하고 버린다. 1초 스냅샷도 즉시 append 한다.
- 실시간 계산 상태는 종목별 bounded deque 하나다. 개수 상한 `MAX_TRADES=4096`, 시간 상한 60초.
- pandas/DataFrame 은 로거 프로세스에 로드되지 않는다. `join.py` 는 장 마감 후 연구용이다.
- gzip 과 일별 집계는 15:30 이후에만, 64KB 청크 스트리밍으로 한다.
- 실측(로컬 서버, 초당 약 5,000 프레임 40초): RSS 33→36MB 에서 평탄하다.

## 구독 (국내 6건 + MU 2건 = 8건, 한 세션)

| TR | 종목 | 내용 |
|---|---|---|
| H0UNCNT0 / H0UNASP0 | 000660 SK하이닉스 | 통합(KRX+NXT) 체결 / 10호가 |
| H0STCNT0 / H0STASP0 | 0193T0 레버리지, 0197X0 인버스 | KRX 체결 / 10호가 |
| HDFSCNT0 | RBAQMU | MU 미국 주간거래(나스닥 BAQ) 실시간 체결 |
| HDFSCNT0 | DNASMU | MU 나스닥(정규/시간외) 체결 — 주간거래 전 아침 구간 |

MU 는 국내 6건 **뒤에** 등록한다. `KIS_LOGGER_MU_ENABLED=0` 이면 등록하지 않는다(기본 ON).
격리: MU 기록 경로의 예외(디스크 오류 포함)는 Engine 이 삼키고 `mu_error` 로 센다 — 하루 50건이면 그날 MU 기록만 끈다.
MU 를 붙인 연결이 30초 안에 끊기기를 3번 연속하면 그날은 MU 등록을 빼고 국내 6건만 다시 붙는다.
production `app/trading/mu_macd` 와 같은 tr_key 지만 로거 전용 appkey 의 별도 세션이다(코드 공유·import 없음).

## 저장 구조 (`$KIS_LOGGER_DATA_DIR`)

| 경로 | 내용 |
|---|---|
| `raw/YYYY-MM-DD/frames_HH.txt` | 수신 원문 `"<수신시각 ms>\t<frame>"`. 장중 무압축 append. 15:30 이후 `.txt.gz` 로 바뀐다 |
| `snapshots/YYYY-MM-DD/<종목>.csv` | 1초 스냅샷(`aggregator.SNAPSHOT_COLUMNS`). 재시작 시 이어 쓰고, 이미 쓴 초는 다시 쓰지 않는다 |
| `logs/YYYY-MM-DD.jsonl` | start / connected / reconnect / disconnect / gap(>5초) / health / maintenance / stop |
| `state/approval_YYYYMMDD.json` | 로거 접속키 캐시 |
| `state/counters_YYYY-MM-DD.json` | 그 날 raw / snapshot / reconnect / gap 누계. 재시작해도 이어서 센다 |
| `mu/YYYY-MM-DD/ticks.csv` | MU 체결 1건 = 1행, 즉시 append. `recv_kst, feed(SYMB), kst_ts(KYMD/KHMS), local_ymd/hms, last, evol, tvol, pbid, pask, vbid, vask, strn, mtyp, lag_s`. 15:30 이후 `.csv.gz` |
| `mu/YYYY-MM-DD/bars_1m.csv` | MU 1분봉 OHLCV(feed 별, KST 분, volume=EVOL 합). 장중엔 확정되는 대로 append(재시작으로 끊긴 분은 complete=0), 15:30 이후 ticks 로 다시 만든다 |
| `mu/YYYY-MM-DD/summary.json` | feed 별 분봉 수·누락률(첫 봉~마지막 봉 사이 체결 없는 분) |
| `daily/YYYY-MM-DD.json` | 일별 요약. 종목별 행 수, 최대 ts 간격, stale(>5초) 비율, 누계, MU 요약 |

## 실행
```
python -m ws_shadow_logger.runner --check                 # 키·데이터경로·RSS 한도 검사 (네트워크 없음)
python -m ws_shadow_logger.runner --live                  # 상주 서비스 (KIS_LOGGER_ENABLED=1 일 때만)
python -m ws_shadow_logger.runner --replay FILE --out DIR # 원문/stub 프레임 재생 → 같은 저장 포맷
python -m ws_shadow_logger.runner --maintain              # 장외 정리를 지금 한 번 (수집시간이면 건너뜀)
python -m pytest -c ws_shadow_logger/pytest.ini --rootdir ws_shadow_logger ws_shadow_logger/tests -q
```

## KIS 공식 기준 (open-trading-api `examples_llm/kis_auth.py`, `kis_devlp.yaml`)

| 항목 | 값 |
|---|---|
| 접속 URL | `ws://ops.koreainvestment.com:21000` (실전) |
| 접속키 | `POST https://openapi.koreainvestment.com:9443/oauth2/Approval` `{"grant_type":"client_credentials","appkey","secretkey"}` → `approval_key` |
| 등록 메시지 | `{"header":{"approval_key","custtype":"P","tr_type":"1","content-type":"utf-8"},"body":{"input":{"tr_id","tr_key"}}}` |
| 등록 한도 | 공식 예제는 40건 초과 시 예외. 이 로거는 **6건**만 등록한다 |
| PINGPONG | 수신하면 `ws.pong(raw)` 로 응답한다(공식 예제와 같다) |

## 1초 스냅샷 컬럼

| 그룹 | 컬럼 |
|---|---|
| 가격·호가 | ts, symbol, price, best_bid, best_ask, spread, spread_bp |
| 잔량 | bid_qty_1..10, ask_qty_1..10, bid_total, ask_total |
| imbalance | imb_1, imb_5, imb_10, imb_total. 정의는 (bid−ask)/(bid+ask) |
| 체결강도 | cttr (KIS CTTR) |
| 체결량 | buy_vol_1s, sell_vol_1s, trades_1s, buy_vol_cum, sell_vol_cum, acml_vol |
| 체결 비중·속도 | buy_ratio_5s/10s, trades_per_s_5/10, vol_per_s_5/10 |
| 최근 1/5/10초 변화 | d_imb1_{1,5,10}s, d_imb5_{1,5,10}s, d_cttr_{1,5,10}s |
| 신선도 | book_age_s, trade_age_s, exch_book_hour, exch_trade_hour |

## MU ↔ 하이닉스 join
MU 분봉 `minute_kst` 와 하이닉스 스냅샷 `ts` 는 둘 다 KST(타임존 없는 ISO)다. `ts[:16] == minute_kst[:16]` 로 같은 분이 맞는다.
MU 분봉의 분은 거래소 체결시각(KYMD/KHMS), 하이닉스 스냅샷은 수신시각이다 — 차이는 ticks.csv 의 `lag_s` 로 확인한다.

## MACD2 결정시점 join (장 마감 후, 연구 전용)
`ws_shadow_logger.join.build(pd.read_csv("dec.csv"), "<KIS_LOGGER_DATA_DIR>")`. 결정시점 이하 스냅샷만 쓰고, 스냅샷 나이가 5초를 넘으면 `data_ok=False` 다. `label` 은 사후 연구 전용이다. predictive test 는 이벤트 A/B/C 각 50건 이상 쌓이기 전에는 하지 않는다.
