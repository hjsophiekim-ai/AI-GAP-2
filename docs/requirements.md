# AI-GAP 프로젝트 요구사항 명세서

버전: 2.0  
작성일: 2026-06-20  
작성자: AI-GAP 개발팀

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| 2.6 | 2026-07-23 | MACD 하이닉스 자동매매 청산: 고정 +3% TP 제거, C PROFIT_LOCK 채택(락 +1.5% net / giveback 0.8pp). MOCK·REAL 동일. CONTINUATION_REENTRY·OPENING_PROBE 기본 OFF 유지 |
| 2.5 | 2026-07-22 | 하이닉스 전략 구조 단순화: A(weighted RANGE)=유일 LIVE 주문, C(MACD+Williams 3분)=episode 확인기(단독 주문 금지), D(가격행동 조기진입)=SHADOW 격리, E=C확인+A주문은 20일 walk-forward에서 A를 이길 때만 LIVE 승격. 하루 데이터 임계값 최적화 금지 |
| 2.4 | 2026-07-21 | 장초반 09:15~09:30 신규진입 금지 블랙아웃 폐지 — 09:00~14:50 전체를 신규진입 허용 구간으로 단순화(중간 금지 구간 없음). 기존 포지션 손절·익절·반전청산·15:15 강제청산은 이전과 동일하게 시간창과 무관하게 항상 실행 |
| 2.3 | 2026-07-20 | 방향판단(000660)과 주문실행 데이터(0193T0/0197X0 실제 ETF) 분리 — 0193T0 전용 1분봉 콜렉터 연결, 신규진입 직전 ETF 자체 데이터 재확인(ETF_DATA_INSUFFICIENT/ETF_DIRECTION_MISMATCH/CHASE_BLOCK/ETF_EXTREME_BLOCK) 게이트 추가 |
| 2.2 | 2026-07-20 | 하이닉스 Enhanced 자동매매 장초반 신규진입 시간창 변경: 09:00~09:10 관망(watch-only) 규칙 삭제, 09:00~09:15 신규진입 허용/09:15~09:30 신규진입 금지/09:30 이후 신규진입 허용으로 대체. 기존 포지션 손절·익절·반전청산·15:15 강제청산은 이 시간창과 무관하게 항상 실행 |
| 2.1 | 2026-07-13 | (요구사항만 반영, 미구현) 하이닉스 자동매매 실거래 기준 손익 계산(NetPnL) 전환 요구사항 추가 |
| 2.0 | 2026-06-20 | 주도섹터 Top3 전략 추가, 미국장 섹터 자동 분석 기능 추가 |
| 1.0 | 2026-06-16 | 최초 작성 (거래량 급증 Top10 전략) |

---

## 1.6 주도섹터 Top3 전략 (Sector Leader Top3 Strategy) ★ 신규

**당일 주도섹터 + 대장주 + 거래대금/거래량 확인 기반 Top3 집중매수 전략.**

> `strategy.mode = "sector_leader_top3"` 설정 시 활성화.

### 데이터 소스

| 소스 | URL | 시간 | 용도 |
|------|-----|------|------|
| NXT 거래대금 | `https://finance.naver.com/sise/sise_quant.naver` | 08:00+ | 주 데이터, 섹터별 거래대금 집계 |
| 거래량 급증 | `https://finance.naver.com/sise/sise_quant_high.naver` | 09:00+ | 보조 확인, volume_spike_confirm_score |
| Yahoo Finance ETF | `https://finance.yahoo.com/quote/{ETF}/` | 전날 | 미국 섹터 강도 자동 분석 |
| 캐시 | `data/cache/us_sector_strength_YYYYMMDD.json` | 24h 유효 | Yahoo 파싱 실패 시 fallback |

### 점수 산식

```
final_score = sector_strength_score(max 35)
            + sector_leader_score(max 25)
            + us_sector_match_score(max 20)
            + volume_spike_confirm_score(max 10)
            + ma_bonus(max 10)
            - risk_penalty(max 30)
```

### 하드 제외 (fallback에서도 절대 복구 금지)

- 현재가 20,000원 미만 / 거래대금 20억 미만
- 상승률 2% 미만 또는 15% 초과
- ETF / ETN / 우선주 / 스팩 / 리츠 / 거래정지

### 미국장 섹터 자동 분석

- **모듈**: `app/services/us_sector_strength_service.py`
- **소스 우선순위**: Yahoo Finance ETF quote → 캐시 → 0점 처리 (프로그램 중단 없음)
- **시장 레짐**: risk_on(SPY/QQQ 모두 양수) | neutral | risk_off(SPY/QQQ 모두 -0.3% 이하)
- **risk_off 시**: us_sector_match_score 50% 축소, UI 경고 표시

### Top3 선정 규칙

1. final_score 상위 3개 선정
2. 동일 섹터 최대 2개 허용
3. 1위: 가장 강한 섹터의 대장주
4. 후보 부족 시 fallback (상승률 1%+, 가격 10,000원+ 완화)

### 신규 모듈

| 파일 | 역할 |
|------|------|
| `app/data/naver_nxt_turnover_collector.py` | NXT 거래대금 수집 (cp949→euc-kr→utf-8 fallback) |
| `app/strategy/sector_mapper.py` | 종목→섹터 매핑 (symbol_overrides > 업종명 > 키워드) |
| `app/strategy/sector_strength_analyzer.py` | 섹터 강도 계산 |
| `app/strategy/sector_leader_top3_selector.py` | Top3 선정 |
| `app/services/us_sector_strength_service.py` | 미국장 섹터 자동 분석 |
| `app/ui/pages/6_주도섹터_Top3.py` | Streamlit UI |
| `config/kr_sector_map.yaml` | 16섹터 종목코드/키워드 매핑 |
| `tests/test_sector_leader_top3.py` | 40개 단위 테스트 |

### 기존 전략 호환

| `strategy.mode` | 사용 전략 |
|----------------|---------|
| `"gap"` | 기존 갭상승 Top15 |
| `"volume_spike"` | 기존 거래량급증 Top10 |
| `"sector_leader_top3"` | **신규** 주도섹터 Top3 |

---

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [전체 프로세스 흐름](#2-전체-프로세스-흐름)
3. [데이터 수집 요구사항](#3-데이터-수집-요구사항)
4. [피처 요구사항](#4-피처-요구사항)
5. [라벨 요구사항](#5-라벨-요구사항)
6. [ML 모델 요구사항](#6-ml-모델-요구사항)
7. [필터링 요구사항](#7-필터링-요구사항)
8. [점수화 요구사항](#8-점수화-요구사항)
9. [Top15 선정 기준](#9-top15-선정-기준)
10. [예산 배분 요구사항](#10-예산-배분-요구사항)
11. [매수 요구사항](#11-매수-요구사항)
12. [매도 요구사항](#12-매도-요구사항)
13. [Streamlit UI 요구사항](#13-streamlit-ui-요구사항)
14. [안전장치 요구사항](#14-안전장치-요구사항)
15. [테스트 요구사항](#15-테스트-요구사항)

---

## 1. 프로젝트 개요

### 1.1 프로젝트 목적

AI-GAP(AI-Guided Automated Portfolio)은 한국 주식시장에서 머신러닝 기반의 종목 선정과 자동화된 매매를 수행하는 시스템이다. 매일 장 시작 전 상위 후보 종목을 선정하고, 장중 자동 매수·매도를 실행하여 수익을 추구한다.

### 1.5 거래량 급증 전략 (Volume Spike Strategy)

**거래량 급증 종목 기반 Top10 전략**이 종목 선정의 기본 전략이다.

#### 1.5.1 기본 원칙

- 기본 전략: 네이버 거래량 급증 종목 페이지 (`https://finance.naver.com/sise/sise_quant_high.naver`) 수집
- 수집 종목 중 **당일 상승률 3% 이상 18% 이하 종목만** 최종 후보로 사용
- 3% 미만 상승: 매수 탄력(수급 탄력) 부족으로 제외
- 18% 초과 상승: 추격매수 위험으로 제외
- **상승률 조건을 벗어난 종목은 Top10 부족 시에도 fallback 복구 금지**

#### 1.5.2 필터 순서

| 순서 | 조건 | 비고 |
|------|------|------|
| 1 | ETF/ETN/KODEX/TIGER/ACE/SOL 등 제외 | 이름 키워드 + 플래그 |
| 2 | 우선주/스팩/리츠 제외 | |
| 3 | 거래정지/관리종목/정리매매 제외 | |
| 4 | 현재가 20,000원 이하 제외 (primary 기준) | |
| 5 | **상승률 3% 미만 제외** (하드 필터, fallback 복구 금지) | excluded_reason: change_rate_below_5 |
| 6 | **상승률 18% 초과 제외** (하드 필터, fallback 복구 금지) | excluded_reason: change_rate_above_15 |
| 7 | 거래대금 30억 이상 → 1차 통과 | primary pass |
| 8 | Top10 부족 시 거래대금 10억 이상 fallback1 (가격 20,000원+, 3~18% 유지) | |
| 9 | Top10 여전히 부족 시 가격 10,000원 이상 fallback2 (거래대금 10억+, 3~18% 유지) | price_relaxed |

#### 1.5.3 점수화 (change_rate_score)

```
final_score = base(5) + change_rate_score + trading_value_score
```

| 상승률 구간 | change_rate_score | 해석 |
|------------|-------------------|------|
| 3% 이상 5% 미만 | +2 | 수급 진입 구간 |
| 5% 이상 8% 미만 | +4 | 안정적인 수급 구간 |
| 8% 이상 12% 이하 | +8 | 최선호 강한 수급 구간 |
| 12% 초과 18% 이하 | +3 | 강하지만 단기 과열 가능성 |
| 3% 미만 또는 18% 초과 | 하드 제외 | — |

#### 1.5.4 fallback 조건 상세

**fallback1 (거래대금 완화)**:
- 현재가 20,000원 초과
- 상승률 3% 이상 18% 이하
- 거래대금 10억 이상

**fallback2 (가격 완화, 1만원 이상)**:
- 현재가 10,000원 이상 (20,000원 미만 포함)
- 상승률 3% 이상 18% 이하
- 거래대금 10억 이상

**절대 복구 금지 (fallback에서도 제외)**:
- 상승률 3% 미만 또는 18% 초과
- 현재가 10,000원 이하
- ETF/ETN/우선주/스팩/리츠/거래정지/관리종목/정리매매

#### 1.5.5 CSV 출력

- Top10 CSV: `data/volume_spike/YYYYMMDD_volume_spike_top10.csv`
  - 컬럼: rank, symbol, name, current_price, change_rate, **change_rate_score**, trade_value, final_score
- 제외 CSV: `data/volume_spike/YYYYMMDD_volume_spike_excluded.csv`
  - excluded_reason: `change_rate_below_5` / `change_rate_above_15` / `price_below_10k` / `etf_etn_or_type`

### 1.2 적용 시장

- 한국거래소(KRX) 코스피(KOSPI) 및 코스닥(KOSDAQ)
- 운용 통화: 대한민국 원화(KRW)

### 1.3 운용 모드

| 모드 | 설명 |
|------|------|
| MOCK | 가상 매매 모드 (실제 주문 없음, 테스트 및 검증 용도) |
| REAL | 실제 매매 모드 (한국투자증권 KIS API 연동) |

### 1.4 핵심 제약조건

- 1일 1회 매수 사이클 (장 개시 후 매수, 당일 또는 익일 청산)
- 종목당 최대 보유 수량: 2주
- 전체 투자 가능 예산 내에서 순환 배분
- ETF, ETN, 우선주, 스팩, 리츠 등 특수 종목 제외

---

## 2. 전체 프로세스 흐름

### 2.1 일일 매매 사이클 개요

```
[데이터 수집]
     |
     v
[피처 생성]
     |
     v
[라벨 생성]
     |
     v
[모델 학습 / 모델 로드]
     |
     v
[예측 (전체 유니버스)]
     |
     v
[후보 50 종목 선정]
     |
     v
[Top 15 종목 선정]
     |
     v
[예산 배분]
     |
     v
[매수 실행]
     |
     v
[보유 관리 (장중 모니터링)]
     |
     v
[일괄 매도 / 조건 매도]
```

### 2.2 시간대별 프로세스

| 시간 | 프로세스 |
|------|---------|
| 장 전 (08:00 ~ 08:50) | 데이터 수집, 피처 생성, 라벨 생성, 모델 학습/갱신, 예측, Top15 선정, 예산 배분 완료 |
| 09:00 ~ 09:10 | 매수 주문 실행 |
| 09:10 ~ 13:00 | 장중 모니터링, 익일 데이터 사전 수집 |
| 13:00 | 미청산 포지션 부분 정리 (필요시) |
| 15:10 | 잔여 포지션 일괄 청산 시작 |
| 15:30 | 장 마감 |

### 2.3 단계별 상세 흐름

#### 단계 1: 데이터 수집
- 네이버증권 시세 데이터 우선 사용
- 장 전: 전일 종가 기준 데이터
- 장 중/후: 실시간 또는 당일 종가 데이터
- KIS API 보조 사용 (주문 체결 데이터, 계좌 정보)

#### 단계 2: 피처 생성
- 기술적 지표 계산 (이동평균, RSI, MACD 등)
- 거래량 관련 지표
- 가격 모멘텀 지표

#### 단계 3: 라벨 생성
- 미래 수익률 기반 이진 분류 라벨
- 학습용 라벨: T+1 ~ T+N 기간 내 목표 수익률 달성 여부

#### 단계 4: 모델 학습
- LightGBM 기반 분류 모델
- 매일 재학습 또는 주기적 재학습

#### 단계 5: 예측
- 전체 유니버스 대상 매수 확률 예측
- 예측 점수 기반 정렬

#### 단계 6: 후보 50 선정
- 예측 점수 상위 50개 종목 선별
- 필터링 규칙 적용 후 50개

#### 단계 7: Top 15 선정
- 후보 50 중 최종 15개 종목 선정
- 복합 점수 기반 랭킹

#### 단계 8: 예산 배분
- 가용 예산을 Top 15 종목에 순환 배분
- 종목당 최대 2주 제한

#### 단계 9: 매수 실행
- 시장가 또는 지정가 주문
- MOCK/REAL 모드 분기 처리

#### 단계 10: 보유 관리
- 장중 포지션 모니터링
- 익절/손절 조건 실시간 감시

#### 단계 11: 일괄 매도
- 장 마감 전 잔여 포지션 청산
- 조건 충족 시 즉시 매도

---

## 3. 데이터 수집 요구사항

### 3.1 데이터 소스 우선순위

| 우선순위 | 소스 | 용도 |
|---------|------|------|
| 1순위 | 네이버증권 | 시세, 재무, 종목 정보 |
| 2순위 | KIS API | 주문, 체결, 계좌 정보 |
| 3순위 | 한국거래소(KRX) | 종목 마스터, 상장 정보 |

### 3.2 네이버증권 데이터 수집

#### 3.2.1 네이버 페이지별 용도 구분

| URL | 용도 |
|-----|------|
| `https://finance.naver.com/sise/sise_quant_high.naver` | **종목 선정 전용** — 거래량 급증 Top10 후보 수집 |
| `https://finance.naver.com/` (일반 페이지) | 현재가 조회, 일별 OHLCV 등 |

> **중요**: 종목 선정 데이터 소스는 반드시 `sise_quant_high.naver`만 사용한다.  
> 구 갭상승 페이지(`item_gap.naver`)는 종목 선정 경로에서 제거되었다.

#### 3.2.2 수집 대상 데이터

- **거래량 급증 종목 선정**: `sise_quant_high.naver` — symbol, name, current_price, change_rate, volume, trade_value
- 일별 OHLCV (시가, 고가, 저가, 종가, 거래량): 일반 네이버 증권 페이지
- 외국인/기관 순매수 데이터
- 시가총액, PER, PBR, ROE 등 재무 지표
- 52주 최고가/최저가

#### 3.2.3 장 전 수집 (08:00 ~ 08:50)

- 전일 종가 기준 OHLCV 데이터
- 최소 60거래일 이상 이력 데이터
- 재무 지표 최신화
- 종목 상장/상폐 변동 반영

#### 3.2.4 장 중 수집

- 실시간 현재가 (1~5분 주기): 네이버 증권 일반 페이지 또는 KIS API
- 거래량 누적 데이터
- 호가 정보 (매수/매도 1~5호가)

#### 3.2.5 장 후 수집

- 당일 종가 확정 데이터
- 체결 강도, 프로그램 매매 동향
- 다음 거래일 준비 데이터 사전 수집

### 3.3 KIS API 데이터 수집

#### 3.3.1 인증 관리

- OAuth2 기반 토큰 인증
- 액세스 토큰 만료 전 자동 갱신 (만료 30분 전)
- MOCK/REAL 모드별 별도 토큰 관리
- 토큰 갱신 실패 시 알림 및 매매 중단

#### 3.3.2 계좌 정보

- 잔고 조회 (현금, 주식 보유 현황)
- 매수 가능 금액 실시간 조회
- 일별 손익 현황

#### 3.3.3 주문 관련

- 주문 접수 및 체결 확인
- 미체결 주문 관리
- 주문 취소/정정

#### 3.3.4 시세 정보

- 현재가 조회
- 호가 조회
- 당일 체결 내역

### 3.4 데이터 품질 요구사항

- 결측값 처리: 거래 정지일 제외, 전일 종가로 대체
- 이상값 탐지: 전일 대비 ±30% 초과 시 플래그 설정
- 데이터 신선도: 피처 생성 전 수집 완료 확인
- 수집 실패 시 재시도 로직: 최대 3회, 지수 백오프

### 3.5 데이터 저장

- 수집된 원시 데이터: `data/raw/` 디렉토리
- 가공 데이터: `data/processed/` 디렉토리
- 모델 입력 데이터: `data/features/` 디렉토리
- 파일 형식: Parquet (압축 효율) 또는 CSV

---

## 4. 피처 요구사항

### 4.1 가격 기반 피처

| 피처명 | 설명 | 파라미터 |
|--------|------|---------|
| SMA_N | 단순이동평균 | N = 5, 10, 20, 60 |
| EMA_N | 지수이동평균 | N = 5, 10, 20 |
| price_return_N | N일 수익률 | N = 1, 3, 5, 10, 20 |
| high_low_ratio | 고가/저가 비율 | - |
| close_open_ratio | 종가/시가 비율 | - |
| price_vs_sma20 | 종가 대비 SMA20 괴리율 | - |
| price_vs_52w_high | 52주 최고가 대비 현재가 | - |
| price_vs_52w_low | 52주 최저가 대비 현재가 | - |

### 4.2 거래량 기반 피처

| 피처명 | 설명 | 파라미터 |
|--------|------|---------|
| volume_ratio_N | N일 평균 거래량 대비 당일 거래량 | N = 5, 20 |
| volume_sma_N | 거래량 이동평균 | N = 5, 20 |
| turnover_rate | 회전율 (거래량/상장주식수) | - |
| volume_price_trend | 거래량-가격 추세 | - |

### 4.3 기술적 지표 피처

| 피처명 | 설명 | 파라미터 |
|--------|------|---------|
| RSI_N | 상대강도지수 | N = 14 |
| MACD | MACD 값 | 12, 26, 9 |
| MACD_signal | MACD 신호선 | - |
| MACD_hist | MACD 히스토그램 | - |
| BB_upper | 볼린저밴드 상단 | 20, 2σ |
| BB_lower | 볼린저밴드 하단 | 20, 2σ |
| BB_width | 볼린저밴드 폭 | - |
| BB_position | 밴드 내 현재가 위치 | - |
| Stochastic_K | 스토캐스틱 %K | 14, 3, 3 |
| Stochastic_D | 스토캐스틱 %D | - |
| ATR_N | 평균진폭 | N = 14 |

### 4.4 모멘텀 피처

| 피처명 | 설명 | 파라미터 |
|--------|------|---------|
| momentum_N | N일 모멘텀 | N = 5, 10, 20 |
| rate_of_change_N | 변화율(ROC) | N = 5, 10 |
| consecutive_up | 연속 상승 거래일 수 | - |
| consecutive_down | 연속 하락 거래일 수 | - |

### 4.5 수급 피처

| 피처명 | 설명 |
|--------|------|
| foreign_net_buy_N | N일 외국인 순매수 합계 |
| institution_net_buy_N | N일 기관 순매수 합계 |
| foreign_holding_ratio | 외국인 보유 비율 |

### 4.6 재무 피처

| 피처명 | 설명 |
|--------|------|
| per | 주가수익비율 |
| pbr | 주가순자산비율 |
| roe | 자기자본이익률 |
| market_cap | 시가총액 (로그 변환) |
| market_cap_rank | 시가총액 순위 (상대적) |

### 4.7 피처 전처리

- 무한값(Inf): NaN으로 대체 후 처리
- 결측값(NaN): 중앙값(median) 또는 0으로 대체
- 정규화: StandardScaler 또는 MinMaxScaler
- 피처 중요도 기반 선택: 상위 N개 피처만 모델 입력

---

## 5. 라벨 요구사항

### 5.1 라벨 정의

#### 5.1.1 기본 이진 라벨

- 목표: T일 매수 후 T+N일까지 수익률 기준 분류
- 양성(1): T+N 기간 내 최대 수익률 >= 목표 수익률
- 음성(0): 그 외

#### 5.1.2 라벨 파라미터

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| 예측 기간 (N) | 5 거래일 | 매수 후 최대 보유 기간 |
| 목표 수익률 | 3% | 양성 판정 기준 수익률 |
| 손절 기준 | -2% | 라벨 무효화 기준 |

### 5.2 라벨 생성 규칙

- 매수가 기준: T일 시가 또는 전일 종가
- 수익률 계산: (최고가 - 매수가) / 매수가 × 100
- 거래 정지일 포함 시 해당 기간 제외
- 상장폐지 종목: 라벨 생성 제외

### 5.3 라벨 불균형 처리

- 양성/음성 비율 확인 (이상적: 30~50% 양성)
- 클래스 가중치 조정: `class_weight='balanced'`
- 오버샘플링: SMOTE 적용 (선택적)

---

## 6. ML 모델 요구사항

### 6.1 모델 선택

#### 6.1.1 주요 모델

- LightGBM (기본 모델): 빠른 학습 속도, 높은 성능
- XGBoost (보조 모델): 앙상블 활용
- RandomForest (백업 모델): 안정성 확보

#### 6.1.2 모델 선택 이유

- 표 형태 금융 데이터에 적합
- 결측값 자체 처리 가능
- 피처 중요도 해석 가능
- 학습 속도 빠름 (일일 재학습 가능)

### 6.2 모델 학습 요구사항

#### 6.2.1 학습 데이터

- 최소 학습 기간: 최근 252 거래일(약 1년)
- 최대 학습 기간: 최근 504 거래일(약 2년)
- 검증 데이터: Walk-Forward Validation

#### 6.2.2 하이퍼파라미터 (LightGBM 기본값)

```
n_estimators: 300
learning_rate: 0.05
max_depth: 6
num_leaves: 50
min_child_samples: 20
subsample: 0.8
colsample_bytree: 0.8
class_weight: 'balanced'
random_state: 42
```

#### 6.2.3 학습 주기

- 매일 재학습: 장 시작 전 수행
- 주 1회 전체 재학습: 토요일 오전
- 재학습 실패 시: 직전 모델 사용 및 알림

### 6.3 모델 평가 요구사항

#### 6.3.1 평가 지표

| 지표 | 최소 기준값 | 설명 |
|------|-----------|------|
| AUC-ROC | 0.55 이상 | 분류 성능 |
| Precision (Top 15) | 0.45 이상 | 상위 예측 정밀도 |
| F1 Score | 0.40 이상 | 정밀도-재현율 균형 |
| 백테스트 승률 | 40% 이상 | 실제 수익 종목 비율 |

#### 6.3.2 모델 저하 감지

- 최근 10 거래일 예측 정확도 모니터링
- AUC-ROC < 0.52 시 재학습 강제 실행
- 연속 3일 손실 발생 시 운용 일시 중단 및 검토

### 6.4 모델 저장 및 관리

- 모델 저장 경로: `models/`
- 파일 형식: joblib 또는 pickle
- 버저닝: 날짜 기반 파일명 (`model_YYYYMMDD.joblib`)
- 최근 30일 모델 보관, 이전 모델 자동 삭제
- 모델 메타데이터 저장: 학습일, 성능 지표, 피처 목록

---

## 7. 필터링 요구사항

### 7.1 종목 제외 기준 (유니버스 필터)

#### 7.1.1 종목 유형 제외

다음 유형의 종목은 투자 대상에서 완전 제외한다.

| 제외 유형 | 식별 방법 | 이유 |
|---------|---------|------|
| ETF | 종목명 포함 키워드: ETF, KODEX, TIGER, KINDEX, KOSEF, ARIRANG, HANARO, TIMEFOLIO | 개별 종목 전략 부적합 |
| ETN | 종목명 포함 키워드: ETN | ETF와 동일 |
| 우선주 | 종목 코드 끝자리 판별 또는 종목명에 '우', '1우', '2우', 'B' 포함 | 유동성 부족, 특수 배당 구조 |
| 스팩(SPAC) | 종목명에 '스팩', 'SPAC' 포함 | 합병 불확실성 |
| 리츠(REITs) | 종목명에 '리츠', 'REIT' 포함 | 부동산 신탁 구조 |
| 인프라펀드 | 종목명에 '인프라', '맥쿼리', '신한알파' 등 포함 | 펀드 구조 |
| 관리종목 | 거래소 관리종목 지정 여부 | 상폐 위험 |
| 투자경고/위험 | 거래소 투자경고/위험 지정 여부 | 급등락 위험 |
| 거래정지 | 당일 거래 정지 종목 | 매매 불가 |

#### 7.1.2 종목명 키워드 필터 목록

```python
EXCLUDE_KEYWORDS = [
    # ETF 운용사
    'KODEX', 'TIGER', 'KINDEX', 'KOSEF', 'ARIRANG', 'HANARO',
    'TIMEFOLIO', 'PLUS', 'ACE', 'KBSTAR', 'SMART', 'TREX',
    # 상품 유형
    'ETF', 'ETN', 'SPAC', '스팩', '리츠', 'REIT', '인프라',
    # 기타
    '선물', '레버리지', '인버스', '합성',
]
```

#### 7.1.3 우선주 판별 로직

```
- 코스피 우선주: 종목 코드 5자리 중 앞 6자리가 보통주 코드와 동일하고, 마지막이 5
- 종목명에 다음 패턴 포함: '우', '1우', '2우', '우B', '1우B', '2우B'
- 예외: '삼성우', 'LG화학우' 형태 모두 포함
```

### 7.2 유동성 필터

| 조건 | 기준값 | 이유 |
|------|--------|------|
| 최소 시가총액 | 300억 원 이상 | 유동성 확보 |
| 최소 일평균 거래량 | 10,000주 이상 (20일 기준) | 체결 가능성 |
| 최소 일평균 거래대금 | 1억 원 이상 (20일 기준) | 대량 매매 가능 |
| 상장 기간 | 상장 후 60 거래일 이상 경과 | 충분한 이력 데이터 |

### 7.3 가격 필터

| 조건 | 기준값 | 이유 |
|------|--------|------|
| 최소 주가 | 1,000원 이상 | 동전주 제외 |
| 최대 주가 | 없음 (제한 없음) | 예산 배분에서 처리 |
| 상한가 종목 제외 | 전일 대비 +29% 초과 | 고점 매수 방지 |
| 하한가 종목 제외 | 전일 대비 -29% 미만 | 급락 종목 제외 |

### 7.4 필터 적용 순서

1. 종목 유형 필터 (ETF/ETN/우선주/스팩/리츠 제외)
2. 관리종목/거래정지/투자경고 필터
3. 유동성 필터 (시가총액, 거래량, 거래대금)
4. 가격 필터 (최소가격, 이상 변동 제외)
5. 데이터 충분성 필터 (충분한 이력 데이터 보유 여부)

---

## 8. 점수화 요구사항

### 8.1 점수 구성 요소

최종 점수는 ML 예측 점수와 규칙 기반 점수를 결합하여 산출한다.

```
최종 점수 = ML점수 × W_ml + 모멘텀점수 × W_momentum + 거래량점수 × W_volume + 재무점수 × W_financial
```

### 8.2 점수 가중치

| 점수 요소 | 기본 가중치 | 설명 |
|---------|-----------|------|
| ML 예측 점수 | 0.50 | LightGBM 매수 확률 |
| 모멘텀 점수 | 0.25 | 가격 모멘텀 종합 |
| 거래량 점수 | 0.15 | 거래량 증가 및 패턴 |
| 재무 점수 | 0.10 | PER, PBR, ROE 기반 |

### 8.3 ML 예측 점수

- LightGBM `predict_proba()` 반환값 (0.0 ~ 1.0)
- 매수 클래스(1)에 대한 확률값 사용
- 추가 보정: Calibration 적용 (선택적)

### 8.4 모멘텀 점수

```
모멘텀점수 = (
    5일수익률 × 0.30 +
    10일수익률 × 0.25 +
    20일수익률 × 0.25 +
    RSI정규화 × 0.20
)
```

- RSI 정규화: RSI 값을 0~1 범위로 변환 (최적 구간: 40~70)
- RSI < 30 또는 RSI > 80: 감점 적용

### 8.5 거래량 점수

```
거래량점수 = (
    5일거래량비율정규화 × 0.50 +
    20일거래량비율정규화 × 0.30 +
    거래대금순위정규화 × 0.20
)
```

- 거래량 급등(5일 평균 대비 3배 초과): 추가 가점
- 거래량 급감: 감점

### 8.6 재무 점수

```
재무점수 = (
    PER정규화역수 × 0.40 +  # 낮을수록 좋음
    PBR정규화역수 × 0.30 +  # 낮을수록 좋음
    ROE정규화 × 0.30         # 높을수록 좋음
)
```

- 재무 데이터 부재 시: 해당 종목 재무 점수 = 0.5 (중립값)

### 8.7 점수 정규화

- 각 점수 요소: Min-Max 정규화 적용 (0.0 ~ 1.0)
- 최종 점수: 0.0 ~ 1.0 범위 보장
- 동점 처리: 거래량 기준 우선 정렬

---

## 9. Top15 선정 기준

### 9.1 선정 프로세스

```
전체 유니버스 (약 2,000~2,500개)
    → 필터링 적용
    → 피처/라벨 생성 가능 종목 (약 1,500~2,000개)
    → ML 예측 실행
    → 후보 50 선정 (점수 상위 50개)
    → 추가 필터링
    → Top 15 선정
```

### 9.2 후보 50 선정 기준

- 최종 점수 기준 상위 50개 종목 선별
- ML 예측 점수 최소 기준: 0.55 이상 (미충족 시 후보 수 감소 허용)
- 동일 업종 집중 방지: 동일 업종 최대 10개 (선택적 적용)

### 9.3 Top 15 최종 선정 기준

#### 9.3.1 기본 선정 규칙

- 후보 50 중 점수 상위 15개 종목
- 연속 N일 보유 종목 재선정 제한: 동일 종목 연속 5 거래일 이상 선정 시 제외 후 재평가

#### 9.3.2 분산 투자 규칙 (선택적)

| 분류 | 최대 종목 수 |
|------|-----------|
| 코스피 | 15개 이내 (상한 없음) |
| 코스닥 | 15개 이내 (상한 없음) |
| 동일 섹터 | 최대 5개 (선택적) |

#### 9.3.3 예산 대비 제외 기준

- 종목당 예산이 1주를 매수하기 불충분한 경우: 해당 종목 제외, 다음 순위 종목으로 대체
- 배분 예산 < 종목 현재가 × 1주: 해당 종목 건너뜀

### 9.4 선정 결과 기록

- 선정 일시, 선정 종목, 점수, 예상 매수 수량 기록
- `logs/top15_YYYYMMDD.json` 형식으로 저장
- Streamlit 대시보드에서 조회 가능

---

## 10. 예산 배분 요구사항

### 10.1 가용 예산 계산

```
가용예산 = 계좌 현금 잔고 × 배분 비율 (기본: 90%)
배분 비율: 0.0 ~ 1.0 설정 가능 (안전 마진 확보)
```

### 10.2 순환 배분 방식

#### 10.2.1 순환 배분 알고리즘

Top 15 종목을 점수 순으로 정렬한 후, 예산이 소진될 때까지 순환하며 각 종목에 1주씩 배분한다.

```
1라운드: 1위 1주 → 2위 1주 → ... → 15위 1주
2라운드: 1위 1주 → 2위 1주 → ... (종목당 최대 2주 제한)
예산 소진 또는 모든 종목 최대 수량 도달 시 종료
```

#### 10.2.2 배분 조건

- 종목당 최대 배분 수량: 2주
- 1회 배분 단위: 1주 (정수 단위 매매)
- 최소 배분 예산: 해당 종목 현재가 이상
- 예산 부족 시: 해당 종목 건너뛰고 다음 종목으로 진행

#### 10.2.3 배분 우선순위

```
동일 라운드 내 배분 우선순위:
1. 최종 점수 높은 종목 우선
2. 점수 동일 시 ML 예측 점수 높은 종목 우선
3. 여전히 동일 시 거래량 많은 종목 우선
```

### 10.3 배분 계산 예시

```
가용예산: 3,000,000원
Top 15 종목 현재가: [10,000, 15,000, 8,000, 20,000, 5,000, ...]

1라운드:
- 1위 (10,000원): 배분 → 잔여 2,990,000원
- 2위 (15,000원): 배분 → 잔여 2,975,000원
- 3위 (8,000원): 배분 → 잔여 2,967,000원
...

2라운드:
- 1위 (10,000원): 1주 추가 배분 (총 2주) → 잔여 예산
- 2위 (15,000원): 1주 추가 배분 (총 2주)
...

종목당 2주 도달 또는 예산 소진 시 종료
```

### 10.4 배분 결과 저장

- 배분 결과: `data/allocation/allocation_YYYYMMDD.json`
- 저장 내용: 종목 코드, 종목명, 배분 수량, 목표 매수가, 배분 금액
- MOCK/REAL 모드별 별도 저장

---

## 11. 매수 요구사항

### 11.1 매수 실행 조건

#### 11.1.1 매수 가능 시간

- 정규 장: 09:00 ~ 15:20
- 권장 매수 시간: 09:00 ~ 09:10 (장 초반 유동성 확보)
- 09:10 이후 매수: 미체결 주문에 한해 재시도

#### 11.1.2 매수 전 사전 확인

- [ ] 계좌 현금 잔고 충분 여부
- [ ] 현재가 정상 조회 여부
- [ ] 종목 거래 중단 여부
- [ ] 당일 이미 해당 종목 보유 여부 (중복 매수 방지)
- [ ] RiskManager 통과 여부

### 11.2 주문 방식

#### 11.2.1 기본 주문 유형

| 방식 | 설명 | 사용 조건 |
|------|------|---------|
| 시장가 주문 | 즉시 체결, 가격 불리 | 유동성 충분한 종목 |
| 지정가 주문 | 목표가 지정, 미체결 가능 | 기본 방식 |

#### 11.2.2 지정가 주문 기준

```
매수 지정가 = 현재가 × (1 + 호가 스프레드 버퍼)
기본 버퍼: 0.5% 이내
호가 단위 반올림 적용
```

#### 11.2.3 미체결 처리

- 미체결 확인 주기: 5분
- 미체결 취소 시간: 09:20 이후 미체결 주문 취소
- 취소 후 처리: 해당 종목 당일 매수 포기, 로그 기록

### 11.3 MOCK 모드 매수

- 실제 주문 API 호출 없음
- 현재가 기준 즉시 체결 가정
- 가상 포지션 파일에 기록: `data/positions/mock_positions.json`
- 가상 잔고 업데이트
- 실제 매매와 동일한 로직 수행 (안전장치 포함)

### 11.4 REAL 모드 매수

- KIS API `주식주문(현금)` 엔드포인트 호출
- 주문 접수 확인: 주문 번호(order_no) 수신 확인
- 체결 확인: 주문 체결 조회 API로 확인
- 주문 결과 기록: `logs/orders/YYYYMMDD_orders.json`

### 11.5 매수 실패 처리

| 실패 유형 | 처리 방법 |
|---------|---------|
| API 호출 오류 | 최대 3회 재시도 (5초 간격) |
| 잔고 부족 | 해당 종목 매수 건너뜀, 다음 종목 진행 |
| 종목 거래 중단 | 매수 취소, 로그 기록 |
| 가격 이상 | 매수 취소, 관리자 알림 |
| 연속 3회 실패 | 매수 프로세스 중단, 알림 발송 |

### 11.6 포지션 기록

```json
{
  "code": "005930",
  "name": "삼성전자",
  "quantity": 2,
  "avg_price": 75000,
  "buy_date": "2026-06-16",
  "buy_time": "09:02:35",
  "mode": "REAL",
  "status": "HOLDING"
}
```

---

## 12. 매도 요구사항

### 12.1 매도 조건 및 우선순위

매도 조건은 다음 우선순위로 적용된다 (위에서 아래로 순서대로 확인):

| 우선순위 | 조건 | 매도 방식 | 비고 |
|---------|------|---------|------|
| 1 | 수익률 >= +5% | 잔여 전량 매도 | 최종 익절 |
| 2 | 수익률 >= +3% (첫 도달) | 절반 매도 (반올림) | 1차 익절 |
| 3 | 수익률 <= -1.5% | 전량 매도 (손절) | 손실 제한 |
| 4 | 13:00 도달 | 조건부 전량 매도 | 선택적 적용 |
| 5 | 15:10 도달 | 잔여 전량 매도 | 일괄 청산 |

### 12.2 익절 상세 규칙

#### 12.2.1 1차 익절 (+3%)

```
조건: (현재가 - 평균매수가) / 평균매수가 >= 0.03
실행: 보유 수량의 절반 매도
  - 2주 보유 시: 1주 매도, 1주 잔류
  - 1주 보유 시: 1주 전량 매도 (잔여 없음)
기록: 1차 익절 플래그 설정 (동일 종목 재발동 방지)
```

#### 12.2.2 2차 익절 (+5%)

```
조건: (현재가 - 평균매수가) / 평균매수가 >= 0.05
   AND 1차 익절 이미 실행됨 (잔여 1주 상태)
실행: 잔여 전량 매도
기록: 포지션 완전 청산
```

#### 12.2.3 익절 단독 경우 (1주만 보유 시)

```
+3% 도달 시 1주 보유 → 전량 매도 처리 (1차 = 최종 청산)
+5% 체크 불필요
```

### 12.3 손절 상세 규칙

```
조건: (현재가 - 평균매수가) / 평균매수가 <= -0.015
실행: 전량 즉시 매도 (1주 또는 2주)
주문 유형: 시장가 주문 (신속 체결 우선)
대기 없이 즉시 실행
```

### 12.4 시간 기반 매도

#### 12.4.1 13:00 매도 (선택적)

```
조건: 오후 1시 도달 AND 해당 포지션 손익 < 0 (손실 상태)
실행: 해당 포지션 전량 매도
비활성화 옵션: 설정으로 13:00 매도 비활성화 가능
```

#### 12.4.2 15:10 일괄 청산

```
조건: 15:10 도달 (장 마감 20분 전)
실행: 모든 잔여 포지션 전량 매도
주문 유형: 시장가 주문
목적: 익일 리스크 제거, 일일 청산 원칙
예외: 별도 익일 보유 허용 설정 시 제외 가능 (기본 비활성)
```

### 12.5 매도 모니터링 주기

- 장중 현재가 조회 주기: 30초 ~ 1분 (설정 가능)
- 익절/손절 조건 확인 주기: 현재가 업데이트마다 실시간 확인
- 시간 기반 매도: 초 단위 정확성 (최대 1분 오차 허용)

### 12.6 MOCK 모드 매도

- 실제 주문 없음
- 현재가 기준 즉시 체결 가정
- 가상 포지션 파일 업데이트
- 가상 손익 계산 및 기록

### 12.7 매도 실패 처리

| 실패 유형 | 처리 방법 |
|---------|---------|
| API 오류 | 즉시 재시도 (최대 5회, 2초 간격) |
| 손절 실패 반복 | 긴급 알림 발송, 수동 처리 요청 |
| 15:10 청산 실패 | 최대 15:20까지 반복 시도 |
| 거래 중단 | 로그 기록 후 다음 체크 시 재시도 |

---

## 13. Streamlit UI 요구사항

### 13.1 전반적 UI 요구사항

- 프레임워크: Streamlit
- 접근 방법: 로컬 실행 (`streamlit run app.py`)
- 화면 갱신: 자동 갱신 주기 설정 가능 (기본: 30초)
- 모드 표시: 현재 MOCK/REAL 모드 항상 상단 표시

### 13.2 페이지 구성

#### 13.2.1 메인 대시보드 (`/`)

- 현재 운용 상태 요약 (모드, 잔고, 오늘 손익)
- 오늘의 Top 15 종목 목록 및 점수
- 현재 보유 포지션 현황
- 미체결 주문 현황
- 시장 상태 (장 개장/마감 여부)

#### 13.2.2 포지션 관리 (`/positions`)

- 현재 보유 종목 목록
- 종목별 매수가, 현재가, 수익률
- 익절/손절 조건 달성 여부 표시
- 수동 매도 버튼 (REAL 모드 확인 필요)

#### 13.2.3 매매 이력 (`/history`)

- 날짜별 매매 내역 조회
- 종목별 손익 현황
- 월별/주별 누적 손익 차트
- 승률, 평균 수익률, 최대 손실 등 통계

#### 13.2.4 모델 현황 (`/model`)

- 최근 모델 성능 지표 (AUC, Precision, F1)
- 피처 중요도 차트 (상위 20개)
- 예측 점수 분포
- 모델 학습 이력

#### 13.2.5 시스템 설정 (`/settings`)

- MOCK/REAL 모드 전환 (비밀번호 확인 필요)
- 투자 예산 및 배분 비율 설정
- 매도 조건 파라미터 설정 (+3%, +5%, -1.5%, 13:00 활성화)
- 알림 설정 (이메일, Slack 등)
- 자동 매매 스케줄 설정

#### 13.2.6 KIS API 연결 상태 (`/kis-connection`)

- MOCK/REAL 토큰 상태 표시 (유효/만료)
- 토큰 수동 갱신 버튼
- API 연결 테스트
- 최근 API 호출 로그

#### 13.2.7 후보 종목 분석 (`/candidates`)

- 후보 50 종목 전체 점수 및 순위
- 종목별 상세 피처 값
- 필터링 제외 이력
- 점수 구성 요소 시각화

### 13.3 UI 기능 요구사항

#### 13.3.1 공통 기능

- 다크/라이트 모드: Streamlit 기본 테마 사용
- 반응형 레이아웃: 와이드 모드 기본 적용
- 데이터 내보내기: CSV 다운로드 버튼
- 새로고침 버튼: 수동 데이터 갱신

#### 13.3.2 알림 기능

- 매수/매도 체결 시 화면 알림 (st.success/st.error)
- 오류 발생 시 즉각 표시
- 손절 발생 시 강조 표시 (빨간색 배경)

#### 13.3.3 접근 제어

- REAL 모드 전환: 비밀번호 확인 필요
- 수동 주문: 추가 확인 팝업
- 설정 변경: 변경 전 확인 단계

### 13.4 성능 요구사항

- 페이지 초기 로딩: 5초 이내
- 데이터 갱신: 2초 이내
- 차트 렌더링: 3초 이내
- 동시 사용자: 1명 (단일 사용자 시스템)

---

## 14. 안전장치 요구사항

### 14.1 RiskManager 요구사항

#### 14.1.1 일일 손실 한도

```
일일 최대 손실 한도: 투자 원금의 3% (설정 가능)
손실 한도 초과 시:
  - 신규 매수 즉시 중단
  - 보유 포지션 조기 청산 (선택적)
  - 관리자 알림 발송
  - 당일 매매 재개 불가 (익일 수동 해제)
```

#### 14.1.2 종목당 최대 손실

```
종목당 최대 손실: 종목 투자금의 5% (손절 -1.5% + 시장가 슬리피지 여유)
실질적으로 손절 조건(-1.5%)이 종목 최대 손실 역할
```

#### 14.1.3 포지션 한도

```
최대 보유 종목 수: 15개 (Top 15 기준)
종목당 최대 수량: 2주
총 투자 비율: 예산의 90% 이내 (현금 10% 항시 유지)
```

#### 14.1.4 연속 손실 경계

```
연속 손실일 감지: 3일 연속 일일 손실 발생 시
처리: 자동 매매 일시 중단, 알림 발송
재개: 수동 확인 후 재개
```

### 14.2 MOCK/REAL 모드 분리

- 포지션 파일 분리: `mock_positions.json` / `real_positions.json`
- 잔고 파일 분리: `mock_balance.json` / `real_balance.json`
- 로그 분리: `logs/mock/` / `logs/real/`
- 모드 혼용 방지: 시작 시 모드 확인, 실행 중 모드 전환 제한

### 14.3 중복 실행 방지

- 프로세스 락 파일: `.lock` 파일로 중복 실행 방지
- 동일 종목 중복 주문: 당일 동일 종목 매수 1회 제한
- 매도 중복 실행: 포지션 없는 종목 매도 시도 차단

### 14.4 데이터 무결성

- 매수 전 현재가 재확인 (캐시된 가격 사용 금지)
- 주문 금액 검증: 배분 금액 = 현재가 × 수량 ± 허용 오차
- 포지션 파일 백업: 매 거래 후 자동 백업
- 계좌 잔고 정합성 확인: 주기적 잔고 동기화

### 14.5 오류 처리 및 복구

#### 14.5.1 시스템 오류

```
API 연결 오류:
  - 재시도: 최대 3회, 지수 백오프 (1s, 2s, 4s)
  - 최대 재시도 초과: 해당 작업 건너뜀, 로그 기록, 알림
  
프로세스 강제 종료:
  - 포지션 파일에서 미청산 포지션 확인
  - 재시작 시 포지션 복원
  - 15:10 이전 재시작 시 매도 감시 재개
```

#### 14.5.2 장애 복구

- 장 중 시스템 재시작: 포지션 파일 기반 상태 복원
- 부분 실행 복구: 매수 완료 종목만 포지션에 반영
- 주문 상태 확인: 재시작 후 미확인 주문 상태 조회

### 14.6 로깅 요구사항

#### 14.6.1 로그 레벨

| 레벨 | 용도 |
|------|------|
| DEBUG | 개발/디버깅용 상세 로그 |
| INFO | 주요 처리 단계 기록 |
| WARNING | 비정상이지만 처리 가능한 상황 |
| ERROR | 처리 실패, 수동 확인 필요 |
| CRITICAL | 시스템 중단 수준 오류 |

#### 14.6.2 로그 파일 관리

- 일별 로그 파일: `logs/YYYY-MM-DD.log`
- 최대 보관 기간: 90일
- 로그 로테이션: 자동 적용
- 주요 이벤트 별도 기록: 매수/매도 이력, 손익 이력

---

## 15. 테스트 요구사항

### 15.1 테스트 유형

#### 15.1.1 단위 테스트 (Unit Test)

| 테스트 대상 | 검증 항목 |
|-----------|---------|
| 필터링 함수 | ETF/우선주/스팩 정확 제외 |
| 피처 생성 | 각 피처 계산 정확성 |
| 라벨 생성 | 라벨 로직 정확성 |
| 점수 계산 | 가중 평균 정확성 |
| 예산 배분 | 순환 배분 알고리즘 정확성 |
| 매도 조건 | 익절/손절 조건 판정 정확성 |

#### 15.1.2 통합 테스트 (Integration Test)

| 테스트 시나리오 | 검증 항목 |
|-------------|---------|
| 전체 파이프라인 실행 | 데이터 수집 → 예측 → 매수까지 오류 없음 |
| MOCK 매수 실행 | 가상 포지션 정확 기록 |
| MOCK 매도 실행 | 익절/손절/일괄 청산 정확 작동 |
| KIS API 연동 | 토큰 갱신, 주문, 체결 확인 |
| 모드 전환 | MOCK↔REAL 전환 시 데이터 혼용 없음 |

#### 15.1.3 MOCK 시뮬레이션 테스트

| 테스트 | 설명 |
|--------|------|
| 정상 익절 시나리오 | +3% 도달 → 절반 매도 → +5% 도달 → 잔여 매도 |
| 손절 시나리오 | -1.5% 도달 → 전량 매도 즉시 실행 |
| 13:00 매도 시나리오 | 손실 상태에서 13:00 도달 → 매도 실행 |
| 15:10 일괄 청산 시나리오 | 모든 보유 종목 15:10에 청산 |
| 예산 부족 시나리오 | 예산 부족 시 적절한 수량 조정 |
| 필터링 경계 테스트 | ETF/우선주 경계 종목 정확 제외 |

#### 15.1.4 백테스트 (Backtesting)

| 항목 | 기준 |
|------|------|
| 백테스트 기간 | 최근 1년 (252 거래일) |
| 데이터 리크 없음 | 미래 정보 사용 여부 확인 |
| 거래 비용 반영 | 수수료 0.015%, 슬리피지 0.1% |
| 평가 지표 | 총 수익률, 승률, Sharpe Ratio, MDD |
| 기준 성과 비교 | KOSPI 지수 대비 초과 수익 여부 |

### 15.2 테스트 환경

| 환경 | 설명 |
|------|------|
| 단위/통합 테스트 | pytest, 로컬 실행 |
| MOCK 시뮬레이션 | 실제 KIS API 불필요, 파일 기반 |
| 백테스트 | 과거 데이터 기반, 별도 백테스트 모듈 |

### 15.3 테스트 자동화

- CI/CD: 코드 변경 시 단위 테스트 자동 실행
- 커버리지 목표: 핵심 로직(필터링, 배분, 매도 조건) 80% 이상
- 테스트 데이터: 고정된 샘플 데이터 사용 (재현 가능)

### 15.4 성능 테스트

| 항목 | 목표 |
|------|------|
| 전체 파이프라인 실행 시간 | 30분 이내 (장 전 완료) |
| 피처 생성 시간 | 10분 이내 (2,000 종목 기준) |
| 모델 학습 시간 | 15분 이내 |
| 예측 시간 | 2분 이내 |
| 매수 주문 실행 시간 | 10초 이내 (15종목) |
| 현재가 조회 및 조건 확인 | 5초 이내 (보유 포지션 전체) |

---

## 부록

### A. 용어 정의

| 용어 | 정의 |
|------|------|
| OHLCV | Open, High, Low, Close, Volume (시가, 고가, 저가, 종가, 거래량) |
| 유니버스 | 투자 대상 전체 종목 풀 |
| 후보 50 | ML 예측 상위 50개 종목 |
| Top 15 | 최종 선정 15개 투자 종목 |
| MOCK 모드 | 가상 매매 테스트 모드 |
| REAL 모드 | 실제 매매 운용 모드 |
| RiskManager | 리스크 관리 모듈 |
| 순환 배분 | 라운드로빈 방식 예산 배분 |
| 1차 익절 | +3% 도달 시 절반 매도 |
| 2차 익절 | +5% 도달 시 잔여 전량 매도 |
| 손절 | -1.5% 도달 시 전량 매도 |
| KIS API | 한국투자증권 Open API |

### B. 주요 설정 파라미터 요약

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| TOP_N_CANDIDATES | 50 | 후보 종목 수 |
| TOP_N_FINAL | 15 | 최종 선정 종목 수 |
| MAX_SHARES_PER_STOCK | 2 | 종목당 최대 보유 수량 |
| BUDGET_RATIO | 0.90 | 가용 예산 비율 |
| TARGET_RETURN_1ST | 0.03 | 1차 익절 수익률 (+3%) |
| TARGET_RETURN_2ND | 0.05 | 2차 익절 수익률 (+5%) |
| STOP_LOSS_RATE | -0.015 | 손절 수익률 (-1.5%) |
| AFTERNOON_SELL_TIME | "13:00" | 오후 조건부 매도 시간 |
| CLOSE_SELL_TIME | "15:10" | 일괄 청산 시간 |
| DAILY_LOSS_LIMIT | 0.03 | 일일 최대 손실 한도 (3%) |
| MIN_MARKET_CAP | 30000000000 | 최소 시가총액 (300억) |
| MIN_VOLUME_20D | 10000 | 최소 20일 평균 거래량 |
| MIN_PRICE | 1000 | 최소 주가 (1,000원) |

### C. 파일 및 디렉토리 구조

```
AI-GAP/
├── app.py                          # Streamlit 메인 앱
├── main.py                         # 파이프라인 실행 진입점
├── config/
│   ├── settings.py                 # 전역 설정
│   └── kis_config.py               # KIS API 설정
├── data/
│   ├── raw/                        # 원시 수집 데이터
│   ├── processed/                  # 가공 데이터
│   ├── features/                   # 피처 데이터
│   ├── allocation/                 # 예산 배분 결과
│   └── positions/                  # 포지션 파일
├── models/                         # 학습된 모델 파일
├── src/
│   ├── data_collector.py           # 데이터 수집 모듈
│   ├── feature_engineer.py         # 피처 생성 모듈
│   ├── label_generator.py          # 라벨 생성 모듈
│   ├── model_trainer.py            # 모델 학습 모듈
│   ├── predictor.py                # 예측 모듈
│   ├── screener.py                 # 필터링 및 점수화 모듈
│   ├── budget_allocator.py         # 예산 배분 모듈
│   ├── order_manager.py            # 주문 관리 모듈
│   ├── position_manager.py         # 포지션 관리 모듈
│   ├── risk_manager.py             # 리스크 관리 모듈
│   └── kis_api.py                  # KIS API 클라이언트
├── pages/                          # Streamlit 멀티페이지
│   ├── 1_positions.py
│   ├── 2_history.py
│   ├── 3_model.py
│   ├── 4_settings.py
│   ├── 5_kis_connection.py
│   └── 6_candidates.py
├── logs/                           # 로그 파일
├── tests/                          # 테스트 코드
└── docs/                           # 문서
    └── requirements.md             # 이 파일
```

---

## 2. 하이닉스 자동매매 — 실거래 기준 손익 계산(NetPnL 전환) ★ 신규(요구사항만, 미구현)

> **상태: 요구사항 등록만 완료, 코드 미구현.** 2026-07-13 사용자 요청으로 본 섹션을
> 추가했다. 아래 내용은 향후 구현 시 그대로 따라야 할 명세이며, 실제 코드
> (`app/trading/trading_cost_engine.py` 등)는 아직 작성되지 않았다.

### 2.0 배경 및 목표

현재(2.0 시점) SK하이닉스⇄SOL 인버스2X 자동매매 시스템의 모든 손익 계산은
**GrossPnL**(매수가-매도가 차이 × 수량)만 반영한다. 실제 한국투자증권(KIS) 계좌
기준으로는 다음이 추가로 반영되어야 한다.

- 매수수수료 / 매도수수료
- 증권거래세(주식) — ETF는 거래세 면제, 대신 다른 보수 구조 적용 가능
- 청산(Clearing) 수수료
- 슬리피지(주문가 vs 실제 체결가 차이)
- 시장가/지정가 주문 구분에 따른 슬리피지 차등

**목표**: 실현손익/미실현손익/Profit Factor/승률/기대값(Expected Value)/백테스트/
실시간 화면에 표시되는 모든 수치를 GrossPnL이 아니라 **NetPnL**(거래비용 차감 후)
기준으로 통일한다.

### 2.1 거래비용 엔진 (신규 모듈)

- 파일: `app/trading/trading_cost_engine.py`
- 클래스: `TradeCostEngine`
- 역할: 종목코드 + 매매방향(BUY/SELL) + 체결가 + 수량 + 주문유형(시장가/지정가)을
  입력받아 수수료/거래세/청산수수료/슬리피지를 계산해 GrossPnL → NetPnL 변환에
  필요한 모든 값을 반환한다.

### 2.2 종목별 거래비용 구분

| 종목코드 | 종목명 | 유형 | 비고 |
|---------|--------|------|------|
| 000660 | SK하이닉스 | 일반 주식 | 증권거래세 적용 |
| 0197X0 | SOL SK하이닉스선물단일종목인버스2X | ETF(ETN) | 거래세 면제, ETF 전용 보수 구조 |

각 종목(또는 종목유형)별로 다음 값을 독립적으로 설정할 수 있어야 한다.

- `commission_buy` (매수수수료율)
- `commission_sell` (매도수수료율)
- `transaction_tax` (증권거래세율 — ETF는 0 또는 별도 계수)
- `clearing_fee` (청산수수료율)

ETF와 일반주식은 종목코드 기반으로 자동 구분한다(예: `0197X0`처럼 ETN/ETF 코드
패턴이거나 별도 종목 마스터 테이블 참조).

### 2.3 한국투자증권 수수료 — 설정 파일 기반(하드코딩 금지)

수수료·세금·슬리피지 값은 코드에 하드코딩하지 않고 `config.yaml` 또는 `.env`에서
읽는다. 예시 키(최종 키 이름은 구현 시 기존 `config.py` 네이밍 컨벤션에 맞춘다):

```yaml
kis:
  domestic_buy_fee_rate: 0.00015      # 국내주식 매수수수료율(예시)
  domestic_sell_fee_rate: 0.00015     # 국내주식 매도수수료율(예시)
  etf_buy_fee_rate: 0.00015           # ETF 매수수수료율(예시)
  etf_sell_fee_rate: 0.00015          # ETF 매도수수료율(예시)
  transaction_tax_rate: 0.0018        # 증권거래세율(일반주식, 예시 — 실제 KIS/거래소 고시값 확인 필요)
  clearing_fee_rate: 0.0             # 청산수수료율(있는 경우)
  slippage_rate_default: 0.0002       # 기본 슬리피지 0.02%
  slippage_rate_market_order: 0.0003  # 시장가 슬리피지(더 크게)
  slippage_rate_limit_order: 0.0001   # 지정가 슬리피지(더 작게)
  min_commission_krw: 0               # 최소수수료(있는 경우)
```

> ⚠️ 위 요율은 예시값이며, 실제 KIS 고시 수수료/거래세율로 구현 시점에 재확인해야
> 한다. `.env`는 계정/키 관련 민감정보 전용 원칙([[feedback_ai_gap_trading_safety]]
> 참고)을 유지하고, 요율처럼 민감하지 않은 값은 `config.yaml` 쪽에 두는 것을
> 우선 검토한다.

### 2.4 슬리피지

- 체결가를 그대로 쓰지 않고, 예상 슬리피지를 반영한 실효 체결가로 손익을 계산한다.
- 기본값: 0.02%(사용자 설정 가능, 2.3의 `slippage_rate_default`).
- 시장가/지정가 주문 유형에 따라 슬리피지율을 다르게 적용한다.

### 2.5 실제 손익 계산 공식

```
GrossPnL = (매도가 - 매수가) × 수량

NetPnL = GrossPnL
         - 매수수수료
         - 매도수수료
         - 증권거래세(해당 종목유형에 한함)
         - 슬리피지
```

### 2.6 Expected Value(기대값) 계산 수정

```
ExpectedValue = 예상이익 - 수수료 - 슬리피지 - 예상세금
```

(기존 `hynix_adaptive_fusion_engine.calculate_expected_value()`가 fees_pct/
slippage_pct를 이미 파라미터로 받고 있으므로, 구현 시 이 값들을 `TradeCostEngine`
계산 결과로 대체하는 방식으로 통합한다.)

### 2.7 진입 조건 — 거래비용 차감 후 기대수익이 남아야 진입

예: `expected_move`가 0.15%인데 왕복 수수료가 0.12%라면, 순기대수익이 사실상
거의 남지 않으므로(또는 음수라면) 진입을 금지한다. 즉 진입 게이트는 항상
`expected_move - round_trip_cost > 0` 같은 순기대수익 기준으로 판단해야 한다.

### 2.8 익절 기준 수정

현재 "+3% 익절"은 Gross 기준이다. 수정 후에는 **수수료·세금 차감 후 순수익이
+3%가 되도록** 익절 트리거 가격을 역산해야 한다(즉, 목표 NetPnL을 먼저 정하고
그에 필요한 GrossPnL 임계가를 계산).

### 2.9 손절 기준 수정

손절도 마찬가지로 **수수료 포함 실손실이 -1.5%가 되도록** 손절 트리거 가격을
역산한다(수수료를 무시하면 실제 계좌 손실은 표시값보다 항상 더 크다는 문제를
해결하기 위함).

### 2.10 UI 표시 항목 추가

거래 관련 화면에 다음 항목을 새로 표시한다.

- 거래비용(합계)
- 매수수수료
- 매도수수료
- 슬리피지
- 세금
- GrossPnL
- NetPnL
- 총 거래비용(수수료+세금+슬리피지 합)

### 2.11 거래내역(Execution Ledger) 컬럼 추가

`app/services/hynix_execution_ledger.py`의 `LEDGER_COLUMNS`에 다음을 추가한다
(기존 2.x대 Adaptive Fusion 컬럼 확장과 동일한 방식으로, 마이그레이션 함수가
기존 행을 보존한 채 헤더만 확장해야 한다).

- `buy_fee`
- `sell_fee`
- `transaction_tax`
- `slippage`
- `gross_pnl`
- `net_pnl`

### 2.12 전략 평가 지표도 NetPnL 기준으로 변경

승률, Profit Factor, Sharpe, MDD 등 모든 성과 지표를 **NetPnL 기준**으로
재계산한다(현재 `compute_performance_stats()`/`hynix_strategy_shadow_tracker`의
승률·PF·MDD 계산이 GrossPnL 성격의 `realized_pnl`/`pnl_krw`를 쓰고 있으므로,
구현 시 이 값들을 NetPnL로 치환해야 한다). Sharpe Ratio는 현재 시스템에 없는
지표이므로 신규 추가가 필요하다.

### 2.13 백테스트 수정

모든 백테스트 경로도 실제 거래비용을 반영하고, 수익률은 **Net Return** 기준으로
계산한다.

### 2.14 거래 회전율(Turnover) 최적화

왕복거래가 과도해 누적 거래비용이 커지면, 자동으로 진입 Threshold를 높여
불필요한(기대수익이 거래비용에 잠식되는) 거래를 줄이는 기능을 추가한다(예: 최근
N회 왕복거래의 누적 거래비용이 누적 GrossPnL의 일정 비율을 넘으면 Threshold를
점진적으로 상향).

### 2.15 설정 화면

사용자가 UI에서 직접 수정 가능해야 하는 항목:

- 수수료율(매수/매도, 일반주식/ETF 구분)
- 슬리피지율(시장가/지정가 구분)
- 종목별 ETF/주식 유형 오버라이드

### 2.16 완료 조건

다음이 모두 **GrossPnL이 아니라 NetPnL 기준**으로 계산되어야 완료로 본다.

- 실현손익
- 미실현손익
- Profit Factor
- 승률
- 기대값(Expected Value)
- 백테스트 결과
- 실시간 화면(UI) 표시값 전체

### 2.17 안전 원칙(기존 프로젝트 원칙 승계)

- `.env` 수정 금지, 실전 마스터 스위치 변경 금지(구현 단계에서도 동일하게 적용).
- mock 모드에서 먼저 검증 후 real 반영 여부를 별도 승인받는다.

---

*문서 끝*
## 2026-07-15 Enhanced SK하이닉스 ETF 자동매매 기본 정책

### 종목 역할

- `SIGNAL_SYMBOL = "000660"`: SK하이닉스. 시세, 갭, VWAP, 1/3/5/15/30분 추세, EMA, 고점/저점, 거래량, PRIMARY_TREND 계산에만 사용한다.
- `LONG_SYMBOL = "0193T0"`: KODEX SK하이닉스단일종목레버리지. HYNIX_BUY, HYNIX_STRONG_BUY, PRIMARY_TREND_UP 실제 매수 종목이다.
- `SHORT_SYMBOL = "0197X0"`: SOL SK하이닉스선물단일종목인버스2X. INVERSE_BUY, INVERSE_STRONG_BUY, PRIMARY_TREND_DOWN 실제 매수 종목이다.
- 000660 직접 매수/매도 주문은 금지한다. 주문 경로에 000660이 들어오면 실패로 처리한다.
- 코드 문자열은 그대로 전달한다. `0193T0`, `0197X0`의 영문자를 제거하거나 숫자형으로 변환하지 않는다.

### 주문 및 스위칭

- 상승 신호는 0193T0 매수, 하락 신호는 0197X0 매수로 실행한다.
- UP↔DOWN 전환은 기존 ETF 전량 매도 체결과 브로커 보유수량 동기화를 확인한 뒤 반대 ETF 매수를 실행한다.
- 주문 접수와 체결을 분리한다. 주문 성공 응답만으로 state/ledger를 갱신하지 않고, KIS 체결/잔고 확인 후 실제 `filled_qty`만 반영한다.
- `POSITION_SYNC_PENDING` 상태에서는 신규주문을 차단한다.
- 0193T0/0197X0 포지션, 원장, 손익은 서로 분리해 관리한다.

### 위험 관리

- 1회 신규 진입은 계좌의 최대 30%까지 허용한다.
- 같은 방향 신호 3회 확인 후에도 최대 50%까지만 확대한다.
- 초기 손절 기준은 -1.0%다.
- 일 손실 -2% 도달 시 신규진입을 중단한다.
- 14:50 이후 신규진입을 금지한다.
- 15:15에는 0193T0과 0197X0의 실제 보유수량을 브로커에서 조회한 뒤 전량청산한다.

### 장초반 신규진입 시간창(2026-07-21 개정 — KST 기준)

기존 "09:00~09:10 관망(watch-only)" 규칙은 2026-07-20에 완전히 삭제됐고, 그
뒤에 도입했던 "09:15~09:30 신규진입 금지" 블랙아웃도 2026-07-21에 폐지했다.
신규진입 허용/금지는 이제 아래 2구간으로만 판단하며,
`app.trading.hynix_switch_risk_gate.is_new_entry_allowed()` 하나가 Early Trend
Detector/ENHANCED_REGIME_SWITCH/Active Strategy/Fast Watcher 등 모든 신규진입
경로가 공유하는 단일 판정 지점이다.

| 시간대(KST) | 신규진입 |
|---|---|
| 09:00 ~ 14:50 | 허용(중간 금지 구간 없음) |
| 14:50 이후 | 금지(기존과 동일) |

- 이 시간창은 신규진입에만 적용된다. 기존 포지션의 손절/익절/반전청산/15:15
  강제청산(`run_liquidation_if_needed`/`run_tp_sl_if_needed`/
  `run_reversal_switch_if_needed`/Dynamic Exit Watcher)은 이 시간창과 무관하게
  항상 실행된다.
- UI는 `describe_new_entry_window()`가 반환하는 현재 허용/금지 상태와 적용 중인
  시간 규칙을 항상 표시한다(모드와 무관).

### UI 표시

- 감시 기초자산: SK하이닉스(000660)
- 상승 실제 거래종목: KODEX SK하이닉스단일종목레버리지(0193T0)
- 하락 실제 거래종목: SOL SK하이닉스선물단일종목인버스2X(0197X0)
- 현재가, 보유수량, 손익은 실제 보유 ETF 기준으로 표시한다.

### 검증 조건

- KIS 현재가/종목명 조회로 다음을 검증한다.
- 0193T0: KODEX SK하이닉스단일종목레버리지
- 0197X0: SOL SK하이닉스선물단일종목인버스2X
- UP 신호 시 0193T0 주문, DOWN 신호 시 0197X0 주문이어야 한다.
- 000660 주문은 어떤 신규주문, 스위칭, 청산 경로에서도 발생하면 안 된다.

## 2026-07-20 추가 확정 규칙: 방향판단(000660)과 주문실행 데이터(ETF) 분리

000660은 Adaptive Regime·큰 방향·추세구조 판단에만 쓴다. 실제 신규진입/확대/
청산 타이밍은 반드시 실제 거래 ETF(0193T0/0197X0) 자신의 1분봉으로 재확인한
뒤에만 실행한다 — 하이닉스(000660) 신호만으로 ETF 주문을 내보내지 않는다.

- `app.data_sources.hynix_long_collector.collect_long_minute()`가 0193T0 자신의
  1분봉을 KIS에서 수집해 `data/cache/hynix_long_minute_1m.csv`에 저장한다.
  000660 분봉(`hynix_minute_1m.csv`)이나 0197X0 분봉(`hynix_inverse_minute_1m.csv`)과
  물리적으로 분리된 별도 파일이며, 어느 쪽도 서로 대체하지 않는다.
- `app.trading.etf_entry_confirmation.confirm_etf_entry()`가 신규진입 직전
  이 데이터로 재확인하는 단일 지점이다 — `run_switch_or_entry()`(모든 신규진입
  경로: ENHANCED_REGIME_SWITCH/Early Trend Detector/Active Strategy/Fast
  Watcher가 공유)가 매수 직전 이 함수를 호출한다.
- 0193T0/0197X0 1분봉이 없거나 부족하거나(5개 미만) 오래됐으면(stale) 그 즉시
  `ETF_DATA_INSUFFICIENT`로 fail-closed 처리해 해당 방향 신규진입을 차단한다.
  캐시나 000660 데이터로 대체하지 않는다.
- 그 외 판정: ETF 자체 VWAP·기울기 방향이 기초자산 방향과 불일치하면
  `ETF_DIRECTION_MISMATCH`, 신호 발생가 대비 0.7% 이상 이동했으면 `CHASE_BLOCK`,
  최근 3분 고점/저점 0.2% 이내(신규 돌파가 아닌 근접)면 `ETF_EXTREME_BLOCK`으로
  차단한다.
- 10/20/30초 단위 기울기는 이 코드베이스에 sub-minute 시세 피드가 없어
  정확히 계산할 수 없다 — 가장 가까운 가용 해상도(1분봉 종가 간)로 근사한다.
- UI(SK하이닉스 자동매매 페이지)는 마지막 신규진입 확인 결과의 데이터 출처,
  stale 여부, 마지막 캔들 시각, 진짜 ETF 데이터 사용 여부, VWAP, 승인/차단
  사유를 항상 표시한다.

## 2026-07-15 추가 확정 규칙: 가격 단위와 주문권한 분리

- 000660 가격은 원 단위 정수 그대로 사용한다. 예: 2,120,000원은 `2120000`이며 `/10`, `*10`, `/100`, `*100` 자동 보정은 금지한다.
- 000660 현재가/전일종가/ATR/VWAP/EMA/지지선/목표가와 0193T0/0197X0 현재가/전일종가/손익 계산 가격은 서로 공유하지 않는다.
- 000660 현재가와 기준 가격이 10배 또는 0.1배 관계이면 `DATA_UNIT_MISMATCH`로 보고 신규 주문을 차단한다. 잘못된 단위 데이터는 캐시에 저장하지 않는다.
- 신규진입과 스위칭 주문 권한은 `ENHANCED_REGIME_SWITCH`만 가진다. `ENHANCED_LEGACY`, Active Strategy, Adaptive Fusion은 Shadow/진단 전용이며 broker buy/sell을 호출하지 않는다.
- `PRIMARY_TREND=UP`이고 VWAP 상단, 15분 추세 UP, 30분 추세 UP, higher low, EMA20 상승 중 2개 이상이면 0197X0 신규매수를 금지한다.
- UP/HYNIX 신호가 조건을 통과하면 실제 주문 종목은 반드시 `0193T0`이다. DOWN/INVERSE 신호가 조건을 통과하면 실제 주문 종목은 반드시 `0197X0`이다.
- 백그라운드 자동매매 사이클 UI의 현재가 카드는 `KODEX 레버리지 현재가(0193T0)`로 표시하고 `last_long_price` 또는 `long_current_price`를 사용한다. 000660 신호가격은 별도 신호/추세 진단 값으로만 표시한다.
## 2026-07-20 Fast Early Order And Exit Rules

Early Trend Detector LIVE uses the 5-second Fast Worker as the direct new-entry order worker. The 3-minute main cycle must not re-submit the same Early entry; it only performs post-entry validation, scale approval, statistics, and UI reporting.

Only `actionable_direction` may drive real orders. `raw_score_leader` and `structural_trend` are diagnostics and sizing/hold-time inputs; they must not create a 0193T0 or 0197X0 buy by themselves.

Signal lifecycle rules:

- Signals older than 60 seconds are discarded as `SIGNAL_EXPIRED`.
- The first entry for the same `signal_id` or `trend_episode` is allowed once.
- Scale-in requires a new `scale_event_id` from swing breakout, VWAP re-breakout, volatility expansion, or ETF volume re-expansion.
- Expected net edge must be at least 0.15% of account value.
- Expected gross profit must be at least 3x estimated round-trip cost.
- `MICRO_CHOP` blocks entries without VWAP or swing breakout, but does not impose a hard trade-count cap.

Fast entry sizing:

- Initial actionable direction confirmation targets roughly 30%.
- Ten seconds of persistence plus both ETF alignment targets roughly 55%.
- Later expansion requires a fresh scale event and meaningful confidence improvement.
- New scale-in is blocked after signal age exceeds 30 seconds.
- `CHASE_BLOCK` applies when the ETF has already moved at least 0.6%, the signal is stale, the entry is near the 1-minute extreme, or the 5-second slope is slowing/reversing.

Exit priority:

- Full exit immediately for hard stop loss, opposite `actionable_direction`, held ETF 5/10/20-second reversal, opposite ETF 5/10-second confirmation, ETF VWAP plus swing structure reversal, PANIC or confirmed regime reversal, or invalidated signal/episode.
- Partial reduction is allowed only for weak 5-second or 10-second opposite noise when 20/30-second direction, VWAP, and swing structure still support the held direction and the opposite ETF is not confirmed.
- After partial reduction, keep the remainder if the original direction recovers within 10 seconds. Exit the remainder if the opposite signal persists for 10-15 seconds or structure breaks.
- Do not immediately re-buy a partially reduced quantity with the old `signal_id`.
- If unrealized net profit is at least +0.8%, lock at least 50% on the first weak opposite signal.
- If giveback from peak profit reaches 0.4-0.6 percentage points, exit fully before the trade returns to flat.
- In `FAST_REVERSAL_RANGE`, weak opposite signals may reduce partially, but strong opposite signals exit fully with a maximum confirmation wait of 10 seconds.
- In `STRONG_TREND`, ignore isolated weak 5-second noise, but do not let structural trend block a full exit when 10/20-second ETF reversal plus VWAP/swing reversal is confirmed.

UI and ledger fields must include `signal_id`, `episode_id`, `scale_event_id`, signal age, detect-to-order-to-fill latency, `MICRO_CHOP`, recent 30-minute PF and move efficiency, expected gross/cost/net edge, partial profit-taking reason, remaining-position hold reason, and final exit reason.

## 2026-07-22 전략 구조 단순화 (A / C / D / E)

4전략 비교(weighted RANGE / MACD 3분 / MACD+Williams / 가격행동 조기진입) 결과를
바탕으로 **새 전략을 추가하지 않고** 역할을 분리한다.

### 역할 분리

| 코드 | 이름 | LIVE 실주문 | 역할 |
|------|------|-------------|------|
| **A** | weighted RANGE | **유일 허용** | 진입·비중·청산의 유일한 broker 결정자 (`evaluate_range_weighted_entry` → `run_switch_or_entry`, `entry_type=WEIGHTED_RANGE_ENTRY`) |
| **C** | 3분봉 MACD + Williams %R | **금지** | `direction_episode` 확인기만. `app.trading.macd_williams_episode.confirm_episode_direction`. 미완성 3분봉 사용 금지 |
| **D** | 가격행동 조기진입 | **금지 (SHADOW)** | 5/10초 slope·VWAP·swing·ETF 상호확인 기반 조기진입은 SHADOW 격리. **실제 5초 틱이 없는 1분봉 선형보간 리플레이로는 활성화하지 않는다** |
| **E** | C 확인 + A 주문 | 조건부 | C로 episode 방향 확인 후 A로 주문·비중·청산. 20거래일 walk-forward에서 A를 안정적으로 이길 때만 LIVE 게이트 승격 |

### C (MACD+Williams) episode 확인 규칙

- MACD histogram(12/26/9)과 Williams %R(14)이 **같은 방향**이면 episode 확인.
- **반대 방향**이면 enhanced 누적점수(`enhanced_score` / `inverse_pressure_score`)가
  해당 episode 방향을 덮어쓰지 못한다 (`enhanced_may_set_direction` = false).
- `broker_order_allowed`는 항상 false — C 단독으로 0193T0/0197X0 주문을 내지 않는다.
- LIVE 게이트 모드: `SHADOW`(기본, 계산·로그만) / `LIVE`(미확인 시 신규진입 차단).
  기본값은 SHADOW이며 `data/state/strategy_e_promotion.json`의 `promote_e_to_live`
  또는 state `macd_williams_episode_gate_mode`로만 LIVE 승격한다.

### D (가격행동) SHADOW 규칙

- `price_action_reversal` / `REVERSAL_CANDIDATE`는 UI·진단·SHADOW 원장용이다.
- LIVE `entry_path_hint`로 `REVERSAL`을 넣지 않는다. Early Probe 단독 broker 경로 금지.
- 가격행동 정보는 **방향 결정에 쓰지 않고** 다음에만 사용한다:
  1. 진입 시점 **5~15초** 미세 조정 (`entry_timing_ok`)
  2. **추격(chase)** 여부 — 0.6% 이상 이동 시 `CHASE_BLOCK` hard block
  3. **ETF 방향 확인** (VWAP / 5·10초 confirm dirs / structure)

### E 승격 조건 (walk-forward)

- 스크립트: `scripts/walkforward_strategy_a_c_e.py` (최소 20거래일).
- 시나리오 비교: **A 단독 / C 단독 / E(C확인+A주문)**.
- **하루 데이터로 임계값을 최적화하지 않는다.**
- 평가 우선순위:
  1. 보수적 Net PnL
  2. PF
  3. MDD
  4. 방향 오류
  5. 거래비용/Gross
  6. 거래 횟수
  7. 15/30초 지연·슬리피지 민감도
- E가 A를 **안정적으로** 이길 때만 `--promote-if-win`으로 LIVE 반영.
  그 전에는 프로덕션 주문은 A만, C는 SHADOW 확인기.

### 구현 모듈

| 파일 | 역할 |
|------|------|
| `app/trading/strategy_architecture.py` | 게이트 모드, SHADOW 페이로드, timing/chase 헬퍼 |
| `app/trading/macd_williams_episode.py` | 3분봉 episode 확인, enhanced override 차단 |
| `scripts/walkforward_strategy_a_c_e.py` | A/C/E 20일 비교 및 승격 파일 기록 |
| `data/state/strategy_e_promotion.json` | `promote_e_to_live` / `episode_gate_mode` |

### 금지 사항

- D 가격행동 조기진입을 LIVE Fast Worker 주문 경로에 다시 연결하는 것.
- C를 단독 실주문 결정자로 승격하는 것.
- 1분봉 선형보간 리플레이로 D를 “검증 통과”로 간주하는 것.
- 당일(또는 1거래일) 결과만으로 E LIVE 승격·임계값 튜닝.

## MACD 하이닉스 자동매매 (Strategy B · MOCK/REAL)

독립 모듈 `app/trading/macd_hynix_*` — signed-B MACD histogram 진입, 전용 워커·원장.
Enhanced / RANGE / Williams episode 게이트와 주문 경로를 공유하지 않는다.

### 진입

- 신호: completed 3분봉 MACD histogram **signed B** (`evaluate_macd_direction`, NEW_TURN_ONLY).
- ETF: UP→`0193T0`, DOWN→`0197X0`. 주문은 sell-confirm-then-buy.
- `CONTINUATION_REENTRY_ENABLED=False` (기본 OFF; 플래그로만 켜짐).
- `OPENING_PROBE_ENABLED=False` (기본 OFF).

### 청산 = C PROFIT_LOCK (고정 +3% TP 없음)

MOCK·REAL 동일 규칙. 워커 틱(≈5초)마다 보유 ETF **실제 시세(mark)** 로 net 수익률을 갱신한다.

| 우선순위 | 조건 | 동작 | `exit_reason` |
|----------|------|------|---------------|
| 1 | 15:00 | 전량 강제청산 | `15:00_FORCE_LIQUIDATE` |
| 2 | 반대 signed-B 신호 | 전량 청산 후 반대 ETF 스위칭 | `OPPOSITE_SWITCH` |
| 3 | net ≤ −1.5% | 전량 손절 | `SL_EXIT` |
| 4 | profit lock | 아래 | `PROFIT_LOCK` |

**Profit lock**

1. net return(vs ETF 진입가, 왕복비용 반영) ≥ **+1.5%** → lock 활성화.
2. lock 활성 후, **peak net return** 대비 giveback ≥ **0.8 percentage points** → 전량 청산 (`PROFIT_LOCK`).
3. 고정 +3.0% take-profit은 **사용하지 않는다**.

원장·UI 필드: `peak_net_return`, `current_net_return`, `giveback_pct`, `profit_lock_active`.

### 구현 모듈

| 파일 | 역할 |
|------|------|
| `app/trading/macd_hynix_strategy.py` | signed B 방향 + profit-lock/SL 헬퍼 |
| `app/trading/macd_hynix_worker.py` | 5초 틱, 청산 우선순위 |
| `app/trading/macd_hynix_order_manager.py` | 주문·state·전용 ledger |
| `app/ui/pages/10_MACD_하이닉스_자동매매.py` | peak/current/giveback/lock UI |

### 금지 사항

- 고정 +3% TP를 live MOCK/REAL에 재도입하는 것.
- 기본값으로 continuation re-entry 또는 opening probe를 다시 켜는 것.

