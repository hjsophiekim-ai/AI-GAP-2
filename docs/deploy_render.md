# Render 배포 가이드

## 서비스 URL

https://ai-gap.onrender.com

---

## Render Web Service 설정

| 항목 | 값 |
|------|----|
| Runtime | Python 3 |
| Region | Oregon (US West) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `streamlit run app/ui/streamlit_app.py --server.address 0.0.0.0 --server.port $PORT` |

> **진입 파일**: `app/ui/streamlit_app.py`  
> 루트에 `app.py`가 없으므로 반드시 위 경로를 사용해야 합니다.

---

## Environment Variables (Render 대시보드에서 설정)

### KIS 모의투자 계좌 (Mock)

| 키 | 설명 |
|----|------|
| `KIS_MOCK_APP_KEY` | KIS 모의투자 앱 키 |
| `KIS_MOCK_APP_SECRET` | KIS 모의투자 앱 시크릿 |
| `KIS_MOCK_ACCOUNT_NO` | 모의투자 계좌번호 (8자리) |
| `KIS_MOCK_ACCOUNT_PRODUCT_CODE` | 모의투자 계좌상품코드 (기본: `01`) |

### KIS 실전투자 계좌 (Real)

| 키 | 설명 |
|----|------|
| `KIS_REAL_APP_KEY` | KIS 실전투자 앱 키 |
| `KIS_REAL_APP_SECRET` | KIS 실전투자 앱 시크릿 |
| `KIS_ACCOUNT_NO` | 실전투자 계좌번호 (8자리) |
| `KIS_REAL_ACCOUNT_PRODUCT_CODE` | 실전투자 계좌상품코드 (기본: `01`) |

### DART 공시 API

| 키 | 설명 |
|----|------|
| `DART_API_KEY` | DART OpenAPI 키 |

### 앱 보안 설정

| 키 | 설명 | 기본값 |
|----|------|--------|
| `APP_PASSWORD` | 앱 접근 비밀번호 (선택) | 없음 |
| `REAL_ORDER_CONFIRM_TEXT` | 실전주문 확인 문구 | `REAL_ORDER_CONFIRMED` |
| `ENABLE_REAL_TRADING` | 실전투자 마스터 스위치 | `false` |
| `ENABLE_REAL_BUY` | 실전 매수 허용 | `false` |
| `ENABLE_REAL_SELL` | 실전 매도 허용 | `false` |
| `DEFAULT_TRADING_MODE` | 기본 거래 모드 | `dry_run` |

> **보안 주의**: 실전투자 관련 환경변수는 Render 대시보드 > Environment 탭에서 설정하고,  
> 절대 코드나 config.yaml에 직접 값을 입력하지 마십시오.

---

## 배포 체크리스트

- [ ] `requirements.txt` 루트에 존재
- [ ] `app/ui/streamlit_app.py` 존재 (Streamlit 진입 파일)
- [ ] `app/ui/pages/` 폴더에 페이지 파일 존재
- [ ] `config.yaml` 루트에 존재 (없으면 안전 기본값 자동 사용)
- [ ] Render 환경변수에 KIS API 키 설정
- [ ] Start Command에 `--server.address 0.0.0.0` 포함 확인

---

## 주요 주의사항

### config.yaml 자동 fallback
`config.yaml`이 없어도 `app/config.py`가 안전 기본값(dry_run 모드, 실전투자 비활성화)으로 자동 동작합니다.  
실전 설정은 Render 환경변수로 override됩니다.

### 실전투자 비활성화 (기본값)
Render 배포 후 기본 모드는 `dry_run`(시뮬레이션)입니다.  
실전투자를 원하면 UI의 **API 연결** 페이지에서 실전모드 버튼을 활성화하고,  
환경변수 `ENABLE_REAL_TRADING=true`, `ENABLE_REAL_BUY=true`를 추가 설정해야 합니다.

### 파일 시스템 제한
Render 무료 플랜은 ephemeral 파일 시스템입니다.  
`data/`, `logs/`, `models/` 디렉토리에 저장되는 CSV/DB/모델 파일은 재배포 시 초기화됩니다.  
영구 저장이 필요하면 Render Disk 또는 외부 스토리지(S3 등)를 사용하세요.

---

## 로컬 검증

```bash
# 의존성 설치
pip install -r requirements.txt

# Python 파일 컴파일 검증
python -m compileall app scripts

# 테스트 실행
pytest

# Streamlit 로컬 실행
streamlit run app/ui/streamlit_app.py
```
