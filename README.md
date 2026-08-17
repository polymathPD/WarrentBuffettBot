# WarrenBuffettBot

한국 주식 역발상(Contrarian) 자동매매 시스템.
개인투자자 과열 신호를 감지하고, Claude AI 에이전트 합의를 거쳐 모의/실전 매매를 실행합니다.

---

## 전체 프로세스 흐름

```mermaid
flowchart TD
    CRON["⏰ APScheduler\n평일 16:10 자동 실행"]

    subgraph COLLECT["1. 데이터 수집"]
        C1["pykrx\n일봉 OHLCV"]
        C2["KIS OpenAPI\n투자자별 매매동향\n(개인/외국인/기관)"]
        C3["KIS OpenAPI\n신용융자 잔고"]
    end

    subgraph SIGNAL["2. 신호 계산"]
        S1["heat_score 산출\n(개인 순매수 + 신용급증 + 거래대금)"]
        S2["역발상 필터\nheat < 7.0\n52주 하위 30%\n3개 지표 모두 존재"]
    end

    subgraph AGENT["3. Claude AI 에이전트 게이트 ← Claude 동작 지점"]
        A1["🤖 market_state\n시장 전체 상태 분석\n⚡ 거부권 보유"]
        A2["🤖 risk\n포트폴리오 리스크 분석\n⚡ 거부권 보유"]
        A3["🤖 retail_flow\n개인 수급 패턴 분석"]
        A4["🤖 credit_heat\n신용 과열 분석"]
        GATE{"4개 에이전트\n전원 통과?"}
    end

    subgraph EXEC["4. 실행 및 기록"]
        E1["모의 매수 체결\n직전 거래일 신호를\n당일 시가로 체결"]
        E2["Railway PostgreSQL\n거래 기록 저장"]
        E3["🌐 웹 대시보드\n포지션 / 수익률 / 설정"]
    end

    subgraph EXIT["5. 청산 조건 (매일 체크)"]
        X1["heat_score ≥ 8.5"]
        X2["보유 20일 초과"]
        X3["손절 -7%"]
    end

    CRON --> COLLECT
    C1 & C2 & C3 --> SIGNAL
    S1 --> S2
    S2 -->|후보 종목| AGENT
    A1 & A2 & A3 & A4 --> GATE
    GATE -->|승인| E1
    GATE -->|반려| REJECT["매수 반려 로그"]
    E1 --> E2 --> E3
    S2 -->|보유 종목| EXIT
    X1 & X2 & X3 --> E1
```

---

## Claude AI 에이전트 역할

| 에이전트 | 분석 내용 | 거부권 |
|---------|----------|--------|
| `market_state` | 시장 전체 하락 종목 비율 — 70% 이상이면 전체 청산 강제 | ✅ |
| `risk` | 슬롯 부족 또는 동일 업종 3개 이상 보유 시 관망 강제 | ✅ |
| `retail_flow` | 개인 순매수 급증 패턴 → 과열 여부 판단 | ❌ |
| `credit_heat` | 신용잔고 급증 → 추가 과열 신호 판단 | ❌ |

- `market_state` 또는 `risk`가 거부하면 즉시 반려
- 나머지 2개는 **2/2 합의** 필요
- 결과는 `agent_decisions` 테이블에 기록 (캐싱으로 동일 입력 재호출 방지)

---

## 기술 스택

| 역할 | 기술 |
|------|------|
| 주가 데이터 | pykrx, FinanceDataReader |
| 수급·신용잔고 | 한국투자증권 KIS OpenAPI |
| 주문 실행 | 한국투자증권 KIS OpenAPI |
| AI 판단 | Anthropic Claude API (claude-sonnet-4-6) |
| DB | Railway PostgreSQL |
| 스케줄러 | APScheduler (평일 16:10) |
| 웹 대시보드 | FastAPI + Jinja2 |
| 배포 | Railway (web + worker) |

---

## 단위 테스트

DB/Claude API/KIS API를 전부 mock으로 격리한 순수 단위 테스트입니다 (실제 네트워크·DB 연결 없이 1~2초 내 완료).

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

- `test_cost_model.py`, `test_signals.py` — 비용모델/heat_score 계산 (순수 함수)
- `test_agents_base.py`, `test_gate.py` — Claude API 캐싱/파싱, 4-에이전트 veto·합의 로직
- `test_contrarian.py` — 진입/청산 후보 필터링, 청산 사유 우선순위
- `test_paper.py` — 모의 매수/매도 비용모델 및 DB 기록
- `test_scheduler.py` — 진입 신호일 선택 (직전 거래일 신호 → 당일 시가 체결)
- `test_universe.py` — 시가총액 기준 수집 대상 선정, 기수집·상장폐지 종목 유지
- `test_investor_flow.py`, `test_credit_balance.py` — KIS 페이지네이션 종료 조건, 커서 기록 조건, 응답 파싱, 종목별 실패 격리
- `test_connection.py` — DB 커넥션 롤백/재연결
- `test_engine.py` — 백테스터 중복 포지션 방지

---

## 실행 방법 (로컬)

```bash
# 의존성 설치
pip install -r requirements.txt

# DB 초기화
python setup_db.py

# 데이터 수집 (순차 실행 — KIS 모의투자 계정은 초당 2건 한도)
python collector/stock_daily.py      # 일봉 (pykrx)
python collector/investor_flow.py    # 투자자별 수급 (KIS)
python collector/credit_balance.py   # 신용잔고 (KIS)

# 신호 계산
python processor/signals.py

# 대시보드 실행
uvicorn dashboard.app:app --reload --port 8000

# 스케줄러 즉시 실행 (테스트)
python scheduler.py --now
```

---

## 환경변수 (.env)

```
DB_URL=postgresql://...
CLAUDE_API_KEY=sk-ant-...
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT=계좌번호
KIS_ACCOUNT_SUFFIX=01
KIS_MOCK=true        # 모의투자 서버 사용 여부
KIS_MODE=paper
```

> 수급·신용잔고 수집 대상은 **시가총액 3,000억 이상**(`collector/universe.py`의 `MIN_MARCAP`)으로
> 제한됩니다. 편도 0.2% 슬리피지 가정으로는 체결할 수 없는 종목을 배제하기 위함이며,
> 이미 수집한 종목은 시총과 무관하게 계속 갱신해 시계열에 구멍이 생기지 않도록 합니다.

> 일봉은 pykrx(인증 불필요), 투자자별 수급과 신용잔고는 KIS OpenAPI로 수집합니다.
> `KIS_APP_KEY`/`KIS_APP_SECRET`이 없으면 수급·신용잔고가 수집되지 않아
> heat_score의 3개 지표 중 거래대금 한 축만 남고, 진입 후보가 나오지 않습니다.

> `KIS_MOCK=true`(모의투자 계정)는 **초당 2건** 요청 한도가 있습니다. 전 종목
> 백필처럼 대량 수집을 할 때는 수집기를 하나씩 순차로 돌리세요.

---

## 백테스트 & 검증 스크립트

과거 데이터로 전략을 검증하거나, heat_score 신호가 실제로 수익률과 관계가 있는지 확인할 때 사용합니다.

```bash
# 1. contrarian_signals 사전 계산 (백테스트 대상 기간)
python backfill_signals.py 2022-01-01 2024-12-31

# 2. 로컬 백테스트 (진입/청산 시뮬레이션 + 기간분리·부트스트랩·무작위대조군 검증)
python run_backtest_local.py 2022-01-01 2024-12-31

# 3. 분위수(decile) 분석 — 문턱값 없이 heat_score/개인수급/거래대금과
#    향후 수익률의 관계 확인, KOSPI 벤치마크 대비 초과수익 비교
python decile_analysis.py 2022-01-01 2024-12-31
```

`backtester/engine.py`(원본 서버사이드 백테스터)도 동일 기능을 제공하지만, DB 디스크 여유가 부족한 환경에서는 `run_backtest_local.py`를 대신 사용하세요 (동일 비용모델 `backtester/cost_model.py`를 그대로 재사용해 결과가 일치합니다).

---

## 전략 연구 기록

전략 검증 과정과 결과는 **[`research/README.md`](research/README.md)** 에 있습니다.

> **현재 전략은 실전 투입 가능한 수준이 아닙니다.** 훈련(2022–2024) / 검증(2025–2026)
> 분리로 다섯 가지 가설을 시험했고 전부 기각됐습니다. 검증 기간 t값이 −1.1 ~ +1.6으로
> 0과 구분되지 않습니다. 판단 근거와 방법론, 실패 사례가 위 문서에 정리돼 있습니다.

```bash
python research/rank_study.py 2022-01-01 2026-08-12   # 랭킹에 신호가 있는가
python research/portfolio_backtest.py 2022-01-01 2024-12-31   # 슬롯 제약 백테스트
python research/factor_deciles.py                     # 팩터 분위수 분석
```

새 가설을 시험할 때는 문서 마지막의 **절차**를 따르세요. 검증 구간을 여러 번 쓰면
사실상 훈련 데이터가 됩니다.

---

## 작업 목록

앞으로 할 작업은 **[`TODO.md`](TODO.md)** 에 있습니다 (대시보드 시각화, 공시·재무 기반
펀더멘털 전략).

---

## 모의 → 실전 전환 기준

- 모의 거래 30건 이상
- 슬리피지 실측 ≤ 0.2%
- MDD ≤ 백테스트 대비 150%

`.env`에서 `KIS_MOCK=false`, `KIS_MODE=live` 로 변경하면 실전 전환됩니다.
