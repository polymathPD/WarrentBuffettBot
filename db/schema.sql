-- 종목 메타 (코드, 종목명)
-- dart_corp_code: DART 조회에 쓰는 8자리 고유번호 (종목코드와 별개 체계)
CREATE TABLE IF NOT EXISTS instruments (
    code           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    dart_corp_code TEXT
);

ALTER TABLE instruments ADD COLUMN IF NOT EXISTS dart_corp_code TEXT;

-- 주식 일봉 OHLCV
CREATE TABLE IF NOT EXISTS stock_daily (
    code TEXT,
    d    DATE,
    o    NUMERIC,
    h    NUMERIC,
    l    NUMERIC,
    c    NUMERIC,
    v    BIGINT,
    PRIMARY KEY (code, d)
);

-- 투자자별 순매수 (개인/외국인/기관)
CREATE TABLE IF NOT EXISTS investor_flow (
    code            TEXT,
    d               DATE,
    individual_net  BIGINT,
    foreign_net     BIGINT,
    institution_net BIGINT,
    PRIMARY KEY (code, d)
);

-- 신용융자 잔고
CREATE TABLE IF NOT EXISTS credit_balance (
    code         TEXT,
    d            DATE,
    credit_amt   BIGINT,
    credit_ratio NUMERIC,
    PRIMARY KEY (code, d)
);

-- 역발상 과열 신호
CREATE TABLE IF NOT EXISTS contrarian_signals (
    code                   TEXT,
    d                      DATE,
    individual_flow_ratio  NUMERIC,
    credit_surge_ratio     NUMERIC,
    volume_ratio           NUMERIC,
    foreign_flow_ratio     NUMERIC,   -- 관측용 (heat_score 미반영)
    institution_flow_ratio NUMERIC,   -- 관측용 (heat_score 미반영)
    credit_ratio_level     NUMERIC,   -- 관측용 (heat_score 미반영)
    heat_score             NUMERIC,
    signal                 TEXT,
    PRIMARY KEY (code, d)
);

-- 기존 DB에 관측용 컬럼 추가 (멱등)
ALTER TABLE contrarian_signals ADD COLUMN IF NOT EXISTS foreign_flow_ratio     NUMERIC;
ALTER TABLE contrarian_signals ADD COLUMN IF NOT EXISTS institution_flow_ratio NUMERIC;
ALTER TABLE contrarian_signals ADD COLUMN IF NOT EXISTS credit_ratio_level     NUMERIC;

-- AI 에이전트 판단 로그
CREATE TABLE IF NOT EXISTS agent_decisions (
    id         SERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ DEFAULT NOW(),
    code       TEXT,
    agent      TEXT,
    score      NUMERIC,
    decision   TEXT,
    rationale  TEXT,
    model      TEXT,
    input_hash TEXT
);

-- 매매 기록
CREATE TABLE IF NOT EXISTS trades (
    id           SERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ DEFAULT NOW(),
    mode         TEXT,
    side         TEXT,
    code         TEXT,
    name         TEXT,
    qty          NUMERIC,
    price        NUMERIC,
    amount       NUMERIC,
    strategy     TEXT,
    agents       JSONB,
    exit_reason  TEXT,
    realized_pct NUMERIC
);

-- 보유 포지션
-- 전략마다 슬롯과 청산 규칙이 다르므로 같은 종목을 두 전략이 각각 보유할 수 있다.
CREATE TABLE IF NOT EXISTS positions (
    code          TEXT,
    strategy      TEXT NOT NULL DEFAULT 'contrarian_v1',
    name          TEXT,
    entry_date    DATE,
    entry_px      NUMERIC,
    qty           NUMERIC,
    stop_px       NUMERIC,
    max_hold_days INTEGER,
    mode          TEXT,
    CONSTRAINT positions_code_strategy_pkey PRIMARY KEY (code, strategy)
);

-- 기존 DB의 positions를 (code) → (code, strategy) 키로 전환 (멱등)
ALTER TABLE positions ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'contrarian_v1';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'positions_code_strategy_pkey'
    ) THEN
        ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_pkey;
        ALTER TABLE positions ADD CONSTRAINT positions_code_strategy_pkey
            PRIMARY KEY (code, strategy);
    END IF;
END $$;

-- DART 공시 목록
CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no  TEXT PRIMARY KEY,   -- 접수번호 (공시 1건의 고유키)
    code      TEXT,
    d         DATE,               -- 접수일자
    report_nm TEXT,
    url       TEXT
);

CREATE INDEX IF NOT EXISTS disclosures_code_d_idx ON disclosures (code, d);

-- DART 주요계정 재무 (분기/사업보고서 단위)
-- EPS/BPS는 주요계정 API에 없다 (전체 재무제표·XBRL을 따로 받아야 함)
--
-- 주의: 손익 3개(revenue/op_income/net_income)는 사업연도 누적치다.
--   2026Q1 = 2026.01~03,  2026Q2 = 2026.01~06,  2025Q4 = 2025.01~12
-- 분기 단독 실적이 필요하면 직전 분기를 빼서 쓴다. 재무상태표 3개
-- (assets/liabilities/equity)는 기말 잔액이라 그대로 비교하면 된다.
CREATE TABLE IF NOT EXISTS financials (
    code        TEXT,
    period      TEXT,      -- 2025Q4 = 2025 사업보고서
    fs_div      TEXT,      -- CFS(연결) 우선, 없으면 OFS(개별)
    revenue     NUMERIC,
    op_income   NUMERIC,
    net_income  NUMERIC,
    assets      NUMERIC,
    liabilities NUMERIC,
    equity      NUMERIC,
    PRIMARY KEY (code, period)
);

-- 수집기 커서 (마지막 수집 시점)
CREATE TABLE IF NOT EXISTS collect_cursor (
    source    TEXT,
    code      TEXT,
    last_seen TIMESTAMPTZ,
    PRIMARY KEY (source, code)
);

-- 사용자 설정 파라미터
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 일별 자산 스냅샷 (일별 수익률·자산곡선의 원천)
CREATE TABLE IF NOT EXISTS equity_daily (
    d               DATE,
    mode            TEXT,
    strategy        TEXT,
    cash            NUMERIC,   -- CAPITAL - 매수금액 + 매도금액
    positions_value NUMERIC,   -- 보유수량 × 기준일 종가
    total_equity    NUMERIC,
    PRIMARY KEY (d, mode, strategy)
);

-- 백테스트 실행 단위 (run_backtest_local.py, research/portfolio_backtest.py 공용)
-- 전략당 최신 결과 한 건만 유지한다. 규칙 변형은 각각 별도 strategy로 부여한다.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id       SERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ DEFAULT NOW(),
    strategy TEXT UNIQUE,
    start_d  DATE,
    end_d    DATE,
    params   JSONB,     -- 슬롯/보유기간/랭킹 지표 등 실행 파라미터
    summary  JSONB      -- 거래수, 평균수익률, t값, MDD 등 요약 통계
);

ALTER TABLE backtest_runs DROP COLUMN IF EXISTS label;
CREATE UNIQUE INDEX IF NOT EXISTS backtest_runs_strategy_key ON backtest_runs (strategy);

-- 백테스트 개별 매매
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id      INTEGER REFERENCES backtest_runs(id) ON DELETE CASCADE,
    code        TEXT,
    entry_d     DATE,
    exit_d      DATE,
    entry_px    NUMERIC,
    exit_px     NUMERIC,
    ret_pct     NUMERIC,   -- 비용 반영 순수익률 (0.012 = +1.2%)
    exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS backtest_trades_run_idx ON backtest_trades (run_id);
