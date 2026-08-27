# Render 배포 가이드

## 서비스 URL

https://ai-gap-2.onrender.com

---

## Render Web Service 설정

| 항목 | 값 |
|------|----|
| Runtime | Python 3 |
| Region | Oregon (US West) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `streamlit run app/ui/streamlit_app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true` |

> **진입 파일**: `app/ui/streamlit_app.py`  
> 루트에 `app.py`가 없으므로 반드시 위 경로를 사용해야 합니다.
>
> **`main.py`를 Start Command로 쓰지 마세요** — `main.py`는 `--script` 필수 인자를 요구하는
> argparse CLI 진입점입니다(예: `python main.py --script app --mode mock`). `streamlit run main.py`나
> `python main.py`처럼 인자 없이 실행하면 즉시 인자 오류로 종료됩니다. Start Command는 반드시 위처럼
> `app/ui/streamlit_app.py`를 직접 실행해야 합니다.
>
> **`--server.headless true`**가 없으면 Streamlit이 로컬 브라우저 자동 실행을 시도하는 등
> 대화형 동작이 활성화되어 헤드리스 컨테이너에서 시작이 불필요하게 지연될 수 있습니다.

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
| `REAL_ORDER_CONFIRM_TEXT` | 실전주문 확인 문구 | `LIVE` |
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
- [ ] Start Command에 `--server.headless true` 포함 확인
- [ ] Start Command가 `main.py`가 아니라 `app/ui/streamlit_app.py`를 직접 실행하는지 확인
- [ ] 배포 로그에서 `STARTUP_STEP_START/DONE/FAILED` 로그로 어느 단계가 느린지/실패했는지 확인 가능

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

`app/utils/data_paths.py`가 이 문제의 표준 해결책입니다 — 환경변수 `AI_GAP_DATA_DIR`이
설정돼 있으면 그 경로(Render Persistent Disk 마운트 경로, 예: `/var/data`)를 데이터
루트로 쓰고, 없으면 프로젝트 로컬 `data/`로 되돌아갑니다. **`data/...` 상대경로를
직접 하드코딩하는 새 코드를 추가하지 마십시오** — 반드시 `app.utils.data_paths`의
`CACHE_DIR`/`STATE_DIR`/`LOGS_DIR` 등을 import해서 씁니다.

**확정 사항 (2026-08-27)**: `ai-gap-2.onrender.com` 서비스는 Persistent Disk가
연결되어 있고 `AI_GAP_DATA_DIR`이 그 마운트 경로를 가리키도록 설정되어 있습니다.
MACD2 신호원장(signal ledger)/거래 실행원장(execution ledger)/`macd2_runtime.json`
등 `data/state`·`data/logs` 하위 파일은 이 Persistent Disk에 저장되며 재배포해도
유실되지 않습니다. (따라서 로컬 저장소의 `data/state/macd2_runtime.json`,
`data/logs/macd2_execution_ledger.csv`는 이 운영 인스턴스와 무관한 로컬
개발/백테스트용 파일이며, 운영 상태 확인은 반드시 Render 인스턴스 쪽에서
해야 합니다.)

---

## 하이닉스/레버리지/인버스 1분봉 자동 저장 (2026-08-18 추가)

`app.services.minute_bar_archive_scheduler`가 `app/ui/streamlit_app.py` 시작 시
(다른 백그라운드 스레드들과 동일하게) 자동으로 뜨는 백그라운드 스레드입니다.
15분마다 깨어나서, 영업일 KST 16:00 이후이고 오늘 날짜가 아직 저장 안 됐으면
`app.services.minute_bar_archiver.run_archive()`를 호출해 000660(하이닉스)/
0193T0(레버리지)/0197X0(인버스) 1분봉을 KIS 주식일별분봉조회로 가져와
`<AI_GAP_DATA_DIR>/cache/replay_<날짜>_{hynix,long,inverse}_1m.csv`에 저장합니다.
SK MACD2와 MU_MACD는 이 세 종목을 동일하게 거래하므로(MU_MACD가 macd2.config의
LONG_SYMBOL/INVERSE_SYMBOL을 그대로 import) 두 모듈에 대해 별도로 데이터를 모을
필요가 없습니다.

안전장치(전부 `tests/test_minute_bar_archiver.py`로 검증됨):
- 요청한 날짜와 KIS가 실제로 반환한 날짜가 다르면(휴장일 등) 저장하지 않음
- 3개 종목 중 하나라도 조회 실패 시 그 날짜 전체를 저장하지 않음(부분 저장 없음)
- 이미 저장된 날짜는 재조회하지 않음(멱등)
- 서버가 재시작돼도 스레드가 다시 뜨고, 최근 10 영업일 안의 누락분을 자동
  보충(`LOOKBACK_CALENDAR_DAYS`)하므로 재시작으로 정확히 16:00 트리거 한 번을
  놓쳐도 다음 체크 주기에 그 날짜가 채워짐
- 모든 실행 결과(성공/실패)는 `<AI_GAP_DATA_DIR>/state/minute_bar_archive_log.json`에
  누적 기록됨

수동 실행/백필(예: 10 영업일보다 오래된 날짜):
```bash
python scripts/save_daily_minute_bars.py                  # 자동 보충
python scripts/save_daily_minute_bars.py 20260601 20260602 # 특정 날짜 지정
```

**주의**: 이 기능은 REAL KIS 클라이언트를 사용합니다(주문은 하지 않는 읽기 전용
과거 시세 조회) — `KIS_REAL_APP_KEY`/`KIS_REAL_APP_SECRET`가 Render 환경변수에
설정돼 있어야 동작합니다.

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
