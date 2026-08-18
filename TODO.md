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
- [x] **B4. 백테스트 뷰** — `/backtest`에서 전략 탭으로 run을 고르면 누적 수익률 곡선,
      청산 사유 분포, 분기별 평균, 거래수·평균·승률·t값과 실행 조건을 보여준다.
      곡선 로직은 `dashboard/templates/_line_chart.html` + `_line_chart_data()`로 index와 공용
- [ ] **B5. 모드 스위치** — `dashboard/app.py`의 `mode` 검증 두 곳(`index`, `trades_page`)에
      backtest 추가

---

## Phase C — 공시·재무·추정치 수집

수집 대상은 `collector/universe.py:target_codes()`를 재사용한다 (시총 3,000억 필터 +
기수집 종목 유지). 수집기 골격은 `collector/investor_flow.py`의 per-code try/except 격리 +
`collect_cursor` 커서 기록 패턴을 따른다.

- [x] **C1. DART corp_code 매핑** — `instruments.dart_corp_code` 컬럼 +
      `collector/dart_corp_code.py`. `corpCode.xml`(zip)을 받아 상장사만 골라 붙인다.
      **실행하려면 `.env`에 `DART_API_KEY`가 필요하다** (opendart.fss.or.kr, 무료)
- [x] **C2. `collector/disclosure.py`** — DART 공시검색 API.
      `disclosures` (rcept_no PK, code, d, report_nm, url).
      종목별이 아니라 기간·시장(유가증권/코스닥)으로 받아 우리 종목만 남긴다.
      `collect_cursor`에 마지막 수집일을 남겨 이어받고, 실패가 있으면 커서를 올리지 않는다
- [x] **C3. `collector/financials.py`** — DART **다중회사** 주요계정(한 번에 100종목).
      `financials` (code+period PK, fs_div, revenue, op_income, net_income,
      assets, liabilities, equity).
      **EPS/BPS는 주요계정 API에 없다** — 필요하면 전체 재무제표(XBRL)를 따로 받아야 한다.
      손익 3개는 사업연도 누적치다 (`db/schema.sql` 주석 참고)
- [~] **C4. `collector/estimates.py`** — 보류. 공시·재무 두 축으로 먼저 전략을 만든다.
      필요해지면 KIS 투자의견 API로 붙인다 (아래 열린 질문 참고)
- [x] 과거분 백필 — 별도 스크립트 없이 수집기에 구간을 넘긴다:
      `python collector/disclosure.py 2022-01-01 2024-12-31`,
      `python collector/financials.py 2024 11011`
- [x] `scheduler.py:daily_job`의 수집 단계에 연결.
      재무는 `collector/financials.py:latest_period()`가 제출 기한을 보고 기간을 고른다

### C5. 발행주식수 (밸류에이션 팩터의 전제)

PBR/PER 같은 밸류에이션은 시가총액 시계열이 있어야 하는데 지금은 없다. 실측으로
소스는 확정해 뒀다.

- 소스: DART **주식의총수현황** `https://opendart.fss.or.kr/api/stockTotqySttus.json`
  (같은 인증키·같은 `dart_corp_code` 사용). 필드는 `istc_totqy`(발행주식총수),
  `tesstk_co`(자기주식), `distb_stock_co`(유통주식)
- 검증: 삼성전자 `istc_totqy` = 5,846,278,608 로 FDR `StockListing('KRX')`의
  `Stocks` 컬럼과 정확히 일치
- **비용 주의**: 다중회사 조회가 없어 종목당 1콜이다.
  3,925종목 × 18분기 = 70,650콜(DART 일 2만 한도로 4일).
  발행주식수는 잘 안 바뀌므로 **사업보고서 기준 연 1회 수집 + 변동 종목만 분기 보강**을
  권한다 (3,925종목 × 5년 ≈ 19,625콜, 하루)
- pykrx 과거 시가총액 경로는 이 환경에서 라이브러리 내부 인코딩 오류로 실패한다

- [x] `collector/shares.py` + `shares(code, period, issued, treasury, floating)` 테이블.
      보통주 행만 쓰고, 이미 저장된 (종목, 기간)은 건너뛰어 중단 후 재개가 된다.
      DART 일일 한도(status 020)를 만나면 즉시 멈춘다
- [ ] 시가총액 = 종가 × 발행주식수, PBR = 시가총액 / 자본총계로 팩터 추가
- [ ] 펀더멘털 전략의 시총 필터를 `shares` 기반으로 바꿔 백테스트에서도 켤 수 있게 하기
      (지금은 FDR 현재 시총이라 실전 경로에서만 켠다)

---

## Phase D — 전략과 에이전트

- [x] **D1. `strategy/fundamental.py`** — `STRATEGY = "fundamental_v1"`.
      실적 보고서 공시일에 전년 동기 대비 영업이익이 개선되고 흑자이며
      ROE ≥ 3% · 부채비율 ≤ 200%인 종목을 개선율 순으로 고른다.
      랭킹은 자본총계 대비 이익 개선폭이다(전년 대비 증가율로 재면 전년 이익이
      0에 가까운 종목이 상위를 독식한다).
      잡주 제외: 최근 20거래일 평균 거래대금 10억 미만 제외 + 시가총액 3,000억 미만 제외.
      시총 필터는 `apply_marcap`으로 끌 수 있고 **백테스트에서는 꺼야 한다** —
      FDR은 현재 시총만 주므로 상장폐지 종목이 통째로 빠져 성과가 부풀려진다.
      청산은 손절 → 만기이고, 최소 보유 5거래일(거래일 기준)은 손절에만 예외를 둔다.
      밸류에이션은 쓰지 않는다 — 시가총액 시계열이 없다(C5 참고)
- [x] **D2. `agents/disclosure.py`, `agents/financials.py`** — `agents/base.py:call()` 재사용.
      공시는 최근 90일 이력에서 증자·관리종목·소송 같은 반대 신호를 찾고,
      재무는 최근 5개 보고서의 영업이익률·ROE·부채비율 추이를 본다.
      재무 프롬프트에는 '손익이 누적치'라는 경고를 넣어 분기 비교 실수를 막는다.
      `agents/risk.py`도 전략별로 슬롯을 세도록 고쳤다 (A3 이후 전 전략을 합산하고 있었다)
- [x] **D3. `agents/gate.py`에 `decide_fundamental()` 추가** — 거부권(market_state·risk)과
      합의 로직을 `_gate()`로 공용화하고, 합의 에이전트만 전략별로 다르게 넘긴다
      (역발상: retail_flow·credit_heat / 펀더멘털: disclosure·financials).
      `agents/risk.py`는 `config.get_setting("SLOTS")`로 런타임 설정을 읽는다
- [x] **D4. 검증** — `research/fundamental_backtest.py`. **기각됐다.**
      훈련(22–24) +5.82%/+3.22% → 검증(25–26) −1.23%/−3.05%로 부호가 뒤집혔고,
      12슬롯 검증은 t −3.22로 유의하게 음수다. 결과와 진단은 `research/README.md` 참고.
      → **Phase E(실전 자동 주문)로 넘어가면 안 된다.** 모의로만 돌린다
- [x] **D5. 모의 실행 연결** — `scheduler.py:daily_job`이 두 전략의 청산·진입을 모두 돈다.
      펀더멘털은 직전 거래일 공시를 오늘 시가로 체결한다(`_prev_trading_day`)

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

- **추정치(컨센서스) 데이터 소스** — 컨센서스 EPS는 무료 공식 API가 없다.
  대안으로 **KIS 투자의견 API**(`/uapi/domestic-stock/v1/quotations/invest-opinion`,
  `.../invest-opbysec`)를 실측했고 모의계좌에서 목표주가·투자의견·증권사가 나온다.
  2022년까지 과거 조회가 되어 백테스트도 가능하다. 제약: 호출당 100건 상한(연 단위로
  잘라도 대형주는 잘림), 투자의견 표기 혼재(`BUY`/`매수`), 백필 약 1.2시간(초당 2건).
  스크래핑(FnGuide·네이버)은 과거 시계열이 없어 검증이 불가능하므로 쓰지 않는다.
- **`CAPITAL` 초기값과 두 전략 간 자본 배분 비율**
- **워크포워드 검증 구간 설계** — 홀드아웃 구간을 이미 쓴 상태에서 어떤 설계를 쓸지
- **기존 역발상 전략을 모의로 계속 돌릴지** — 현재 검증 실패 상태
  (`research/README.md` 참고)
