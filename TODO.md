# TODO

앞으로 할 작업 목록. 두 갈래로 나뉜다.

1. **시각화 UI** — 백테스트/모의/실전의 일별 수익률, 자산배분, 매매정보를 대시보드에서 본다.
2. **펀더멘털 전략** — 공시·재무·추정치를 수집해 Claude 에이전트가 매수/매도를 판단하고
   주문까지 자동 실행한다. 기존 heat_score 역발상 전략과 **독립 병행**한다.

전제:

- 수집·판단·주문은 기존 16:10 배치(`scheduler.py:daily_job`)에 붙인다. 장중 폴링은 하지 않는다
- 새 전략은 실전 주문까지 자동화한다. Phase E의 안전장치가 먼저 들어가야 한다
- 운용 제약: 전략당 동시 5종목, 최소 보유 1주(5거래일), 시총 3,000억 이상

---

## Phase A — 기반 (UI보다 먼저)

### A1. 백테스트 결과 영속화

현재 `run_backtest_local.py`와 `research/portfolio_backtest.py`는 결과를 stdout으로만 낸다.
대시보드에서 보려면 DB에 남아야 한다.

- [x] `db/schema.sql`에 테이블 추가 (기존 `CREATE TABLE IF NOT EXISTS` 멱등 패턴 유지)
  - `backtest_runs` (id, ts, strategy UNIQUE, start_d, end_d, params JSONB, summary JSONB)
  - `backtest_trades` (run_id, code, entry_d, exit_d, entry_px, exit_px, ret_pct, exit_reason)
- [x] `backtester/store.py:save_run()` — 두 스크립트 공용 저장 함수.
      전략당 결과 한 건만 유지하고 재실행 시 매매까지 교체한다
- [x] `run_backtest_local.py` — 실행 결과를 위 두 테이블에 기록
- [x] `research/portfolio_backtest.py` — 규칙 변형마다 별도 strategy로 기록
- [x] 요약 통계는 `recorder/evaluator.py`의 `mdd()`, `bootstrap_positive_rate()` 재사용
- [x] 비용 계산은 `backtester/cost_model.py` 그대로 사용

### A2. 자본금 기반 포지션 사이징

`executor/paper.py:buy()`의 `qty = 1` 고정 때문에 자산배분 개념이 성립하지 않는다.

- [x] `config._DEFAULTS`에 `CAPITAL` 추가 (기본 1,000만원, settings 테이블로 런타임 변경)
- [x] `executor/sizing.py:position_qty()` — 모의/실전 공용 수량 계산
- [x] `executor/paper.py:buy()` — `qty = floor(CAPITAL / SLOTS / entry_px)`
- [x] `executor/live.py:buy_and_record()` — 동일 계산으로 정렬 (수량 인자 제거)
- [x] `db/schema.sql`에 `equity_daily` (d, mode, strategy, cash, positions_value, total_equity)
- [x] `recorder/equity.py:snapshot()` — `scheduler.py:daily_job` 마지막 단계에서 호출

### A3. 전략 구분

`positions`에 전략 컬럼이 없어 두 전략이 슬롯과 청산 규칙을 공유하게 된다
(`trades`에는 `strategy`가 이미 있다).

- [x] `positions`에 `strategy` 컬럼 추가. 기본키도 `(code, strategy)`로 바꿔
      두 전략이 같은 종목을 각각 보유할 수 있게 한다
- [x] `strategy/contrarian.py`의 보유 종목 조회·청산 후보 조회에 전략 조건 반영
- [x] `executor/paper.py`·`executor/live.py`의 슬롯 확인 COUNT를 전략별로 계산.
      매수/매도 함수는 `strategy`를 인자로 받는다 (하드코딩 제거)

---

## Phase B — 시각화 UI

서버사이드 Jinja2 + 인라인 SVG 방식을 유지한다 (`dashboard/app.py:_pnl_chart_data` 패턴,
외부 JS 라이브러리 없음). `templates/base.html` 사이드바와 `static/css/style.css`
디자인 시스템에 맞춘다.

- [x] **B1. 일별 자산곡선** — `dashboard/app.py:_equity_chart_data()`가 `equity_daily`를
      모드 단위로 합산해 날짜 x축 곡선 + 일별 수익률 막대를 그린다
- [x] **B2. 자산배분** — `dashboard/app.py:_allocation_data()`가 보유 종목별 평가금액과
      현금을 스택 바 + 비중 표로 낸다. 색상 슬롯 7개를 넘으면 '기타'로 묶는다
- [x] **B3. 매매 정보** — `/trades`에 기간·구분·청산사유 필터.
      `trades.agents` JSONB(에이전트별 결정·확신·이유)를 `<details>` 펼침으로 노출.
      설정 화면에 `CAPITAL` 입력 추가
- [ ] **B4. 백테스트 뷰** — `/backtest` 라우트 신설. run 목록 → 선택 시 자산곡선,
      청산 사유 분포, 분기별 수익률, t값·MDD·승률
- [ ] **B5. 모드 스위치** — `dashboard/app.py`의 `mode` 검증 두 곳(`index`, `trades_page`)에
      backtest 추가

---

## Phase C — 공시·재무·추정치 수집

수집 대상은 `collector/universe.py:target_codes()`를 재사용한다 (시총 3,000억 필터 +
기수집 종목 유지). 수집기 골격은 `collector/investor_flow.py`의 per-code try/except 격리 +
`collect_cursor` 커서 기록 패턴을 따른다.

- [ ] **C1. DART corp_code 매핑** — `instruments`에 `dart_corp_code` 컬럼 추가.
      DART `corpCode.xml`을 내려받아 종목코드 ↔ 고유번호 매핑 적재.
      `DART_API_KEY`는 `config.py`에 이미 예약돼 있다
- [ ] **C2. `collector/disclosure.py`** — DART 공시검색 API.
      `disclosures` (code, rcept_no PK, d, report_nm, url)
- [ ] **C3. `collector/financials.py`** — DART 단일회사 주요계정.
      `financials` (code, period PK, revenue, op_income, net_income, equity, debt, eps, bps)
- [ ] **C4. `collector/estimates.py`** — 소스 미정. 아래 열린 질문 참고.
      소스를 정하지 못하면 이 축을 빼고 C2·C3만으로 진행한다
- [ ] 과거분 백필 스크립트 (DART는 과거 공시 조회가 되므로 백테스트가 가능하다)
- [ ] `scheduler.py:daily_job`의 수집 단계에 연결

---

## Phase D — 전략과 에이전트

- [ ] **D1. `strategy/fundamental.py`** — `STRATEGY = "fundamental_v1"`.
      진입 후보 생성 규칙(공시 이벤트 + 재무 필터)과 청산 규칙.
      최소 보유 5거래일 규칙 포함 (손절만 예외)
- [ ] **D2. `agents/disclosure.py`, `agents/financials.py`** — `agents/base.py:call()` 재사용.
      입력 해시 캐싱과 `결정: 매수|관망|청산 / 확신: 0~10 / 이유:` 출력 형식 유지
- [ ] **D3. `agents/gate.py`에 `decide_fundamental()` 추가** — 거부권/합의 구조는
      기존 `decide()`와 같은 형태
- [ ] **D4. 검증** — `research/README.md`의 절차를 따른다. 2025–2026 홀드아웃은 기존
      다섯 가설에 이미 소진됐으므로 워크포워드 설계가 필요하다.
      실전 자동화 전에 백테스트를 한 번은 통과해야 한다

---

## Phase E — 실전 자동 주문

`executor/live.py`의 `buy_and_record()` / `sell_and_record()`는 구현돼 있고 스케줄러에
연결만 안 돼 있다. 연결 전에 아래를 먼저 만든다.

- [ ] **E1. 킬 스위치** — settings 테이블에 `LIVE_ENABLED`. 기본 off,
      대시보드 `/settings`에서 토글
- [ ] **E2. 주문 한도** — 일일 주문 건수 상한, 1종목 최대 금액, 전략별 슬롯 5
- [ ] **E3. 중복 주문 방지** — 같은 날 재실행 시 중복 진입을 막는 조건
- [ ] **E4. KIS 응답 필드 검증** — `executor/live.py`가 잔고 조회 응답에서 읽는
      `pchs_avg_pric`, `prpr` 필드명을 모의투자로 먼저 확인
- [ ] **E5. 전환 기준 확인** — 모의 30건 / 슬리피지 실측 ≤ 0.2% /
      MDD ≤ 백테스트 대비 150%를 측정한 뒤 `KIS_MOCK=false`
- [ ] **E6. 스케줄러 연결** — E1~E5 완료 후 `scheduler.py:daily_job`에 실전 실행 경로 추가

---

## 열린 질문

- **추정치(컨센서스) 데이터 소스** — 무료 공식 API가 없다. 유료 API, 스크래핑, 생략 중 선택
- **`CAPITAL` 초기값과 두 전략 간 자본 배분 비율**
- **워크포워드 검증 구간 설계** — 홀드아웃 구간을 이미 쓴 상태에서 어떤 설계를 쓸지
- **기존 역발상 전략을 모의로 계속 돌릴지** — 현재 검증 실패 상태
  (`research/README.md` 참고)
