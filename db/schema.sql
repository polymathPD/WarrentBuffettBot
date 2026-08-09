-- 종목 메타 (코드, 종목명)
CREATE TABLE IF NOT EXISTS instruments (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

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
    code                  TEXT,
    d                     DATE,
    individual_flow_ratio NUMERIC,
    credit_surge_ratio    NUMERIC,
    volume_ratio          NUMERIC,
    heat_score            NUMERIC,
    signal                TEXT,
    PRIMARY KEY (code, d)
);

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
CREATE TABLE IF NOT EXISTS positions (
    code          TEXT PRIMARY KEY,
    name          TEXT,
    entry_date    DATE,
    entry_px      NUMERIC,
    qty           NUMERIC,
    stop_px       NUMERIC,
    max_hold_days INTEGER,
    mode          TEXT
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
