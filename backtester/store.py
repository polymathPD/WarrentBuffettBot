"""
백테스트 결과 저장.

run_backtest_local.py와 research/portfolio_backtest.py가 같은 두 테이블
(backtest_runs / backtest_trades)에 기록해 대시보드에서 함께 조회한다.

전략당 결과는 한 건만 남긴다. 규칙을 바꾼 백테스트는 이전 결과를 덮어쓰므로,
비교하려는 규칙 변형에는 각각 다른 strategy 이름을 준다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import psycopg2.extras
import db.connection as db


def save_run(strategy: str, start_d: str, end_d: str,
             params: dict, summary: dict, trades: list[dict]) -> int:
    """
    실행 한 건과 그에 딸린 매매를 저장하고 run_id를 반환한다.
    같은 strategy의 기존 결과가 있으면 매매까지 통째로 교체한다.

    trades 각 원소는 code / entry_d / exit_d / entry_px / exit_px / ret_pct /
    exit_reason 키를 갖는다. 날짜는 date 또는 'YYYY-MM-DD' 문자열, ret_pct는
    비율(0.012 = +1.2%)이다.
    """
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO backtest_runs
                   (strategy, start_d, end_d, params, summary)
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                   ON CONFLICT (strategy) DO UPDATE
                   SET ts = NOW(), start_d = EXCLUDED.start_d, end_d = EXCLUDED.end_d,
                       params = EXCLUDED.params, summary = EXCLUDED.summary
                   RETURNING id""",
                (strategy, start_d, end_d,
                 json.dumps(params, default=str),
                 json.dumps(summary, default=str)),
            )
            run_id = cur.fetchone()[0]

            cur.execute("DELETE FROM backtest_trades WHERE run_id = %s", (run_id,))
            if trades:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO backtest_trades
                       (run_id, code, entry_d, exit_d, entry_px, exit_px, ret_pct, exit_reason)
                       VALUES %s""",
                    [(run_id, t["code"], t["entry_d"], t["exit_d"], t["entry_px"],
                      t["exit_px"], t["ret_pct"], t["exit_reason"]) for t in trades],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"  백테스트 저장: {strategy} (run_id={run_id})  매매 {len(trades):,}건")
    return run_id
