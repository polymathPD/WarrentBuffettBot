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
-- 투자자별 순매수
--
-- 단위: 원(KRW). 반드시 지킬 것.
--   KIS의 *_ntby_tr_pbmn 필드는 '백만원'이고 *_ntby_qty는 '주'다. 수집기가
--   collector/investor_flow.py의 PBMN_TO_WON으로 원으로 환산해 넣는다.
--   과거에 이 환산이 없어 참조 DB에서 이관한 원 단위 행과 10^6배 어긋났고,
--   processor/signals.py의 flow_ratio(오늘/30일 평균)가 창 하나에 두 단위를
--   물면서 heat_score가 전 종목 0으로 죽었다. 단위를 섞으면 신호가 조용히
--   사라지고 백테스트까지 무의미해진다.
CREATE TABLE IF NOT EXISTS investor_flow (
    code            TEXT,
    d               DATE,
    individual_net  BIGINT,   -- 원
    foreign_net     BIGINT,   -- 원
    institution_net BIGINT,   -- 원
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
-- 전략마다 슬롯과 청산 규칙이 다르고, 같은 전략도 paper 시뮬레이션과 live 실계좌
-- 두 벌을 나란히 굴린다. PK에 mode가 빠져 있으면 live INSERT가 ON CONFLICT에서
-- paper 행을 만나 qty만 덮어쓰고 mode는 'paper'로 남는다 — 대시보드의 live 탭이
-- 비어 보이는 원인이었다.
CREATE TABLE IF NOT EXISTS positions (
    code          TEXT,
    strategy      TEXT NOT NULL DEFAULT 'contrarian_v1',
    name          TEXT,
    entry_date    DATE,
    entry_px      NUMERIC,
    qty           NUMERIC,
    stop_px       NUMERIC,
    max_hold_days INTEGER,
    mode          TEXT NOT NULL,
    CONSTRAINT positions_code_strategy_mode_pkey PRIMARY KEY (code, strategy, mode)
);

-- 기존 DB의 positions를 (code) → (code, strategy) → (code, strategy, mode)로
-- 점진 전환 (멱등). 현재 PK 컬럼 조합을 읽어 부족하면 재설치한다.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'contrarian_v1';
-- 진입 판단은 포지션에 붙는다. trades에만 두면 체결을 못 본 날 판단까지 사라진다
-- (2026-08-24 오리온홀딩스·영원무역홀딩스: 실제로 샀는데 기록이 통째로 없었다).
ALTER TABLE positions ADD COLUMN IF NOT EXISTS agents JSONB;
DO $$
DECLARE
    pk_cols TEXT;
BEGIN
    SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
    INTO pk_cols
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
    WHERE c.conrelid = 'positions'::regclass AND c.contype = 'p';

    IF pk_cols IS DISTINCT FROM 'code,strategy,mode' THEN
        UPDATE positions SET mode = 'paper' WHERE mode IS NULL;
        ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_pkey;
        ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_code_strategy_pkey;
        ALTER TABLE positions ALTER COLUMN mode SET NOT NULL;
        ALTER TABLE positions ADD CONSTRAINT positions_code_strategy_mode_pkey
            PRIMARY KEY (code, strategy, mode);
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

-- 발행주식수 (시가총액·PBR의 전제)
-- 시가총액 = 종가 × issued. 자기주식을 뺀 유통주식(floating)도 함께 둔다.
CREATE TABLE IF NOT EXISTS shares (
    code     TEXT,
    period   TEXT,      -- financials와 같은 표기 (2025Q4 = 2025 사업보고서)
    issued   BIGINT,    -- 발행주식의 총수(보통주)
    treasury BIGINT,    -- 자기주식
    floating BIGINT,    -- 유통주식
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
