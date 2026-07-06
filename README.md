# AI-GAP 갭상승 자동매매 프로그램

## 개요

AI-GAP은 매일 장 시작(09:00~09:10) 직후 갭상승한 국내 주식 중 **Top 15 종목을 선정**하여 자동으로 매수·매도를 수행하는 자동매매 시스템입니다.

- **데이터 수집**: 네이버 금융 및 한국투자증권(KIS) OpenAPI에서 갭상승 종목 데이터를 수집합니다.
- **ML 예측**: 과거 갭상승 데이터로 학습된 RandomForest 모델로 수익 가능성을 예측합니다. 모델이 없으면 룰 기반 점수(rule_score)로 대체합니다.
- **자동 매수/매도**: 예산 배분 후 장 중 익절(+3%, +5%), 손절(-1.5%), 시간 기반 강제매도(13:00, 15:10)를 수행합니다.

> **주의**: 이 프로그램은 투자 수익을 보장하지 않습니다. 주식 투자에는 원금 손실 위험이 있습니다.

> **기본 모드**: 프로그램의 기본 실행 모드는 `dry_run`(시뮬레이션)입니다. 실제 주문은 발생하지 않으며 화면 출력만 이루어집니다.

---

## 갭상승 Top15 선정 프로세스

```
                    ┌──────────────────────────────────────────────────────────┐
                    │           갭상승 Top15 선정 파이프라인                    │
                    └──────────────────────────────────────────────────────────┘

  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────────────┐
  │ 1. 데이터   │    │ 2. 필터링   │    │ 3. 피처     │    │ 4. ML 예측       │
  │    수집     │───>│             │───>│    생성     │───>│ (없으면          │
  │             │    │             │    │             │    │  rule_score)     │
  │ 네이버/KIS  │    │ ETF/ETN 제외│    │ 갭율, 거래  │    │                  │
  │ 갭상승탭    │    │ 우선주 제외 │    │ 강도, 상대  │    │ RandomForest     │
  │ 장중 현재가 │    │ 스팩/리츠   │    │ 모멘텀 등   │    │ ml_weight=0.6    │
  └─────────────┘    │ 투자경고 등 │    │ 다차원 피처 │    │ rule_weight=0.4  │
                     │ 가격 1000원 │    └─────────────┘    └──────────────────┘
                     │ 거래대금    │                                │
                     │ 30억 이상   │                                │
                     └─────────────┘                                │
                                                                     ▼
  ┌─────────────┐    ┌──────────────────────────────────────────────────────┐
  │ 6. Top15    │    │ 5. 후보 50 생성 (final_score 내림차순)               │
  │    선정     │<───│                                                      │
  │             │    │ - 시가대비 -1.5% 이하 종목 제외                      │
  │ 섹터 분산   │    │ - 갭율 15% 초과 종목 우선순위 후순위 이동            │
  │ (섹터당     │    │ - 상위 50개 후보 저장 (data/candidates/YYYYMMDD_     │
  │  최대 3개)  │    │   candidate50.csv)                                   │
  │ 최종 15개  │    └──────────────────────────────────────────────────────┘
  │ 선정 및    │
  │ 순위 부여  │
  └─────────────┘
```

---

## 폴더 구조

```
AI-GAP/
├── app/                          # 핵심 애플리케이션 코드
│   ├── config.py                 # 설정 로더 (config.yaml + .env)
│   ├── logger.py                 # 로깅 설정
│   ├── models.py                 # 데이터 모델 (StockData, Candidate, BuyPlan 등)
│   ├── data/                     # 데이터 수집 모듈
│   │   ├── data_collector.py     # 통합 데이터 수집기
│   │   ├── kis_market_data.py    # 한국투자증권 API 데이터 수집
│   │   ├── naver_gap_collector.py # 네이버 금융 갭상승탭 스크래핑
│   │   └── sample_data.py        # 테스트용 샘플 데이터
│   ├── features/                 # 피처 엔지니어링
│   │   ├── feature_builder.py    # 학습/예측용 피처 생성
│   │   └── label_builder.py      # 학습 레이블 생성
│   ├── ml/                       # 머신러닝 모듈
│   │   ├── train_model.py        # RandomForest 모델 학습
│   │   ├── predict_model.py      # 학습된 모델로 예측
│   │   └── model_store.py        # 모델 저장/불러오기
│   ├── strategy/                 # 종목 선정 전략
│   │   ├── filters.py            # ETF/ETN, 우선주 등 필터링
│   │   ├── scoring.py            # 룰 기반 점수 계산
│   │   ├── candidate_generator.py # 후보 50 생성 파이프라인
│   │   └── top15_selector.py     # 후보 50 → Top15 선정
│   ├── trading/                  # 매매 실행 모듈
│   │   ├── broker_base.py        # 브로커 기본 인터페이스
│   │   ├── broker_factory.py     # 모드별 브로커 생성 팩토리
│   │   ├── budget_allocator.py   # 예산 배분 (순환 배분)
│   │   ├── dry_run_broker.py     # 드라이런 브로커 (주문 없음)
│   │   ├── mock_broker.py        # 모의투자 브로커 (KIS 모의계좌)
│   │   ├── real_broker.py        # 실전투자 브로커 (KIS 실계좌)
│   │   ├── order_manager.py      # 주문 관리
│   │   ├── portfolio.py          # 포트폴리오 관리
│   │   └── sell_manager.py       # 매도 전략 실행
│   ├── storage/                  # 데이터 저장소
│   │   ├── csv_store.py          # CSV 파일 저장/불러오기
│   │   └── database.py           # SQLAlchemy DB (선택 사항)
│   ├── utils/                    # 유틸리티
│   │   ├── stock_utils.py        # 주식 관련 유틸리티
│   │   └── time_utils.py         # 시장 시간 관련 유틸리티
│   └── ui/                       # Streamlit UI
│       ├── streamlit_app.py      # 메인 앱 진입점
│       └── pages/
│           ├── 1_데이터수집_및_모델학습.py  # 1단계: 데이터 수집/학습
│           ├── 2_Top15_종목선정.py          # 2단계: Top15 선정
│           ├── 3_예산배분_및_매수.py        # 3단계: 예산배분 및 매수
│           └── 4_보유종목_및_일괄매도.py    # 4단계: 보유종목 및 매도
├── data/                         # 런타임 데이터 (자동 생성)
│   ├── candidates/               # 후보 50 CSV (YYYYMMDD_candidate50.csv)
│   ├── selected/                 # Top15 CSV (YYYYMMDD_top15.csv)
│   └── orders/                   # 매수 계획 CSV (YYYYMMDD_buy_plan.csv)
├── models/                       # 학습된 ML 모델 (자동 생성)
│   ├── gap_model.pkl             # RandomForest 모델
│   └── feature_importance.csv    # 피처 중요도
├── logs/                         # 로그 파일
├── reports/                      # 학습 리포트 (train_report_YYYYMMDD.txt)
├── scripts/                      # CLI 실행 스크립트
│   ├── run_dry_buy.py            # 드라이런 매수 실행
│   └── run_select_top15.py       # Top15 선정만 실행
├── tests/                        # 테스트 코드
├── config.yaml                   # 전략 및 시스템 설정 파일
├── .env                          # API 키 환경변수 (직접 생성 필요)
├── .env.example                  # .env 예시 파일
└── requirements.txt              # Python 의존성 패키지
```

---

## 설치 방법

### 요구 사항

- Python 3.11 이상
- 한국투자증권 계좌 및 OpenAPI 키 (모의투자 또는 실전투자)

### 패키지 설치

```bash
pip install -r requirements.txt
```

설치되는 주요 패키지:

| 패키지 | 버전 | 용도 |
|--------|------|------|
| streamlit | >=1.32.0 | 웹 UI |
| pandas | >=2.0.0 | 데이터 처리 |
| numpy | >=1.26.0 | 수치 연산 |
| scikit-learn | >=1.4.0 | RandomForest ML 모델 |
| requests | >=2.31.0 | HTTP 요청 |
| beautifulsoup4 | >=4.12.0 | 네이버 금융 스크래핑 |
| lxml | >=5.1.0 | HTML 파싱 |
| PyYAML | >=6.0.1 | config.yaml 파싱 |
| python-dotenv | >=1.0.0 | .env 환경변수 로드 |
| joblib | >=1.3.0 | 모델 직렬화 |
| sqlalchemy | >=2.0.0 | DB 저장 (선택) |
| pytest | >=8.0.0 | 단위 테스트 |

---

## 설정 방법

프로젝트 루트의 `config.yaml` 파일에서 전략 파라미터를 조정합니다.

### 주요 항목 설명

```yaml
mode: "mock"  # 실행 모드: dry_run | mock | real
              # dry_run: 시뮬레이션 (주문 없음, 기본값)
              # mock: 한국투자증권 모의투자 계좌 사용
              # real: 한국투자증권 실전투자 계좌 사용

trading:
  total_budget: 10000000       # 총 투자 예산 (원)
  max_positions: 15            # 최대 보유 종목 수
  max_shares_per_stock: 2      # 종목당 최대 매수 주수
  min_gap_rate: 3.0            # 최소 갭상승률 (%)
  max_gap_rate: 15.0           # 최대 갭상승률 (%)
  min_trade_value: 3000000000  # 최소 거래대금 (30억 원)
  buy_start_time: "09:05"      # 매수 시작 시각
  buy_end_time: "09:10"        # 매수 종료 시각
  first_take_profit_rate: 3.0  # 1차 익절 기준 (+3%)
  second_take_profit_rate: 5.0 # 2차 익절 기준 (+5%)
  stop_loss_rate: -1.5         # 손절 기준 (-1.5%)
  force_sell_time: "13:00"     # 강제 매도 시각 (오후 1시)
  emergency_sell_time: "15:10" # 긴급 매도 시각 (장 마감 전)
  order_type: "limit"          # 주문 유형 (limit: 지정가)
  min_price: 1000              # 최소 주가 (원)

filters:
  exclude_etf: true            # ETF 제외
  exclude_etn: true            # ETN 제외
  exclude_preferred_stock: true # 우선주 제외
  exclude_spac: true           # 스팩 제외
  exclude_reit: true           # 리츠 제외
  exclude_warning_stock: true  # 투자경고 종목 제외
  exclude_halt: true           # 거래정지 종목 제외

ml:
  use_model: true              # ML 모델 사용 여부
  fallback_to_rule_score: true # ML 모델 없을 시 룰 기반 점수 사용
  ml_weight: 0.6               # ML 점수 가중치
  rule_weight: 0.4             # 룰 점수 가중치

safety:
  enable_real_trading: false   # 실전투자 활성화 여부 (기본: false)
  require_real_confirm: true   # 실전투자 시 확인 문구 입력 요구
  real_confirm_text: "I_UNDERSTAND_REAL_TRADING_RISK"
  max_real_order_amount: 1000000    # 실전 1회 최대 주문금액
  max_real_daily_budget: 1000000    # 실전 일일 최대 투자금액
  max_daily_loss_rate: -5.0         # 일일 최대 손실률 (%)
```

---

## 한국투자증권 API 키 설정

프로젝트 루트에 `.env` 파일을 생성하고 API 키를 입력합니다. (`.env.example` 참고)

```dotenv
# 모의투자 계좌 (mock 모드)
KIS_MOCK_APP_KEY=발급받은_모의투자_APP_KEY
KIS_MOCK_APP_SECRET=발급받은_모의투자_APP_SECRET
KIS_MOCK_ACCOUNT_NO=12345678-01
KIS_MOCK_ACCOUNT_PRODUCT_CODE=01

# 실전투자 계좌 (real 모드)
KIS_REAL_APP_KEY=발급받은_실전투자_APP_KEY
KIS_REAL_APP_SECRET=발급받은_실전투자_APP_SECRET
KIS_ACCOUNT_NO=12345678-01
KIS_ACCOUNT_PRODUCT_CODE=01

# DART 공시 API (선택 사항)
DART_API_KEY=발급받은_DART_API_KEY
```

> API 키는 [한국투자증권 OpenAPI 포털](https://apiportal.koreainvestment.com/)에서 발급받을 수 있습니다.
> `.env` 파일은 절대로 git에 커밋하지 마십시오. `.gitignore`에 이미 등록되어 있습니다.

---

## 실행 방법

### Streamlit 앱 실행 (권장)

```bash
streamlit run app/ui/streamlit_app.py
```

브라우저에서 `http://localhost:8501`로 접속합니다. 사이드바에서 현재 모드(DRY RUN / MOCK / REAL)와 시장 상태를 확인할 수 있습니다.

### 드라이런 CLI 실행

```bash
python scripts/run_dry_buy.py
```

실제 주문 없이 매수 시뮬레이션 결과를 터미널에 출력합니다.

### Top15 선정만 실행

```bash
python scripts/run_select_top15.py
```

데이터 수집부터 Top15 선정까지만 실행하고 결과를 `data/selected/YYYYMMDD_top15.csv`에 저장합니다.

---

## 4단계 사용 방법

Streamlit UI는 매매 프로세스를 4단계 페이지로 구성합니다.

### 1단계: 데이터 수집 및 모델 학습

- 네이버 금융 및 KIS API에서 갭상승 종목 데이터를 수집합니다.
- 과거 데이터가 충분히 쌓이면(기본 500행 이상) RandomForest 모델을 학습합니다.
- 학습된 모델은 `models/gap_model.pkl`에 저장되고, 학습 리포트는 `reports/` 폴더에 생성됩니다.
- 데이터가 부족한 경우 룰 기반 점수(rule_score)만으로 선정합니다.

### 2단계: Top15 종목 선정

- 수집된 갭상승 종목에 필터링, 피처 생성, 점수 계산을 적용합니다.
- 상위 50개 후보(candidate50)를 먼저 생성한 뒤 Top15를 선정합니다.
- 선정 결과는 `data/selected/YYYYMMDD_top15.csv`에 저장됩니다.

### 3단계: 예산배분 및 매수

- Top15 종목에 예산을 순환 배분(round-robin) 방식으로 배분합니다.
- 09:05~09:10 사이에 지정가 매수 주문을 실행합니다.
- 매수 계획은 `data/orders/YYYYMMDD_buy_plan.csv`에 저장됩니다.

### 4단계: 보유종목 및 일괄매도

- 현재 보유 종목 현황과 손익을 실시간으로 표시합니다.
- 익절/손절 조건 충족 시 자동 매도합니다.
- 13:00 강제매도, 15:10 긴급매도로 당일 포지션을 정리합니다.

---

## 예산 배분 방식

예산 배분은 **순환 배분(round-robin)** 방식을 사용합니다.

예시 (총 예산 1,000만 원, 종목당 최대 2주):

```
[1라운드] 종목1 1주, 종목2 1주, 종목3 1주, ... (각 종목에 1주씩 순서대로 배분)
[2라운드] 종목1 1주 추가, 종목2 1주 추가, ... (예산이 남는 한 계속)
```

- 주가가 비싼 종목도 고르게 포함될 수 있습니다.
- 예산 부족으로 배분받지 못한 종목은 매수 계획에서 제외됩니다.
- `config.yaml`의 `trading.max_shares_per_stock`으로 라운드 수를 조정합니다.

---

## ETF/ETN 제외 기준

다음 키워드를 포함하는 종목명은 자동으로 제외됩니다:

```
KODEX, TIGER, ACE, SOL, PLUS, KBSTAR, KOSEF, HANARO, ARIRANG,
ETN, ETF, 레버리지, 인버스, 선물, 합성, TR, RISE, FOCUS, TREX,
TIMEFOLIO, WOORI
```

이 외에도 다음 유형의 종목이 제외됩니다:

| 제외 유형 | 기준 |
|-----------|------|
| 우선주 | 종목명 끝이 '우', '우B', '숫자우', '숫자우B' 패턴 |
| 스팩(SPAC) | 종목명에 '스팩' 또는 '숫자호스팩' 포함 |
| 리츠(REITs) | 종목명에 '리츠' 또는 'reits' 포함 |
| 투자경고 | is_warning 플래그 또는 is_halt(거래정지) 플래그 |
| 저가주 | 주가 1,000원 미만 |
| 소형주 | 거래대금 30억 원 미만 |

---

## 매도 전략

| 조건 | 설명 |
|------|------|
| **1차 익절** | 매수 대비 +3% 도달 시 매도 |
| **2차 익절** | 매수 대비 +5% 도달 시 매도 |
| **손절** | 매수 대비 -1.5% 하락 시 매도 |
| **강제 매도** | 13:00 기준으로 보유 중인 모든 종목 매도 |
| **긴급 매도** | 15:10 장 마감 전 잔여 포지션 전량 매도 |

> 손절선(-1.5%)은 갭상승 종목 특성상 시가 대비 하락이 빠를 수 있으므로, 빠른 손절로 손실을 제한합니다.

---

## 실전투자 주의사항

실전투자를 활성화하려면 다음 두 가지 조건을 모두 만족해야 합니다.

**1. `config.yaml` 수정**

```yaml
safety:
  enable_real_trading: true   # false → true 로 변경
```

**2. Streamlit UI에서 안전 확인 문구 입력**

실전 모드 실행 전, 다음 문구를 직접 입력해야 합니다:

```
I_UNDERSTAND_REAL_TRADING_RISK
```

추가 안전 장치:
- `max_real_order_amount`: 실전 1회 주문 최대 금액 (기본 1,000,000원)
- `max_real_daily_budget`: 실전 일일 최대 투자 금액 (기본 1,000,000원)
- `max_daily_loss_rate`: 일일 최대 손실률 초과 시 자동 중단 (기본 -5.0%)

---

## Render 배포

서비스 URL: **https://ai-gap.onrender.com**

자세한 설정은 [docs/deploy_render.md](docs/deploy_render.md)를 참조하세요.

### 핵심 설정

| 항목 | 값 |
|------|----|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `streamlit run app/ui/streamlit_app.py --server.address 0.0.0.0 --server.port $PORT` |

### 필수 환경변수 (Render 대시보드 > Environment)

```
KIS_MOCK_APP_KEY          KIS_REAL_APP_KEY
KIS_MOCK_APP_SECRET       KIS_REAL_APP_SECRET
KIS_MOCK_ACCOUNT_NO       KIS_ACCOUNT_NO
KIS_MOCK_ACCOUNT_PRODUCT_CODE   KIS_REAL_ACCOUNT_PRODUCT_CODE
DART_API_KEY              REAL_ORDER_CONFIRM_TEXT
ENABLE_REAL_TRADING       ENABLE_REAL_BUY
ENABLE_REAL_SELL          DEFAULT_TRADING_MODE
```

---

## 테스트 실행

```bash
pytest
```

특정 테스트만 실행하려면:

```bash
pytest tests/test_filters.py -v
pytest tests/test_scoring.py -v
pytest tests/test_budget_allocator.py -v
```

---

## 면책 문구

이 프로그램은 교육 및 연구 목적으로 제공됩니다.

- **투자 손실에 대한 책임은 전적으로 사용자에게 있습니다.**
- 이 프로그램의 개발자 및 기여자는 투자 결과에 대해 어떠한 법적·재정적 책임도 지지 않습니다.
- 주식 투자는 원금 손실의 위험이 있으며, 과거 수익률이 미래 수익률을 보장하지 않습니다.
- 실전투자 전 반드시 모의투자(mock 모드)로 충분히 테스트하시기 바랍니다.
- 자동매매 프로그램 사용 중에도 시장 상황을 직접 모니터링하는 것을 권장합니다.
