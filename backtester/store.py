"""
백테스트 결과 저장.

run_backtest_local.py와 research/portfolio_backtest.py가 같은 두 테이블
(backtest_runs / backtest_trades)에 기록해 대시보드에서 함께 조회한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import db.connection as db


def save_run(strategy: str, label: str, start_d: str, end_d: str,
             params: dict, summary: dict, trades: list[dict]) -> int:
    """
    실행 한 건과 그에 딸린 매매를 저장하고 run_id를 반환한다.

    trades 각 원소는 code / entry_d / exit_d / entry_px / exit_px / ret_pct /
    exit_reason 키를 갖는다. 날짜는 date 또는 'YYYY-MM-DD' 문자열, ret_pct는
    비율(0.012 = +1.2%)이다.
    """
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO backtest_runs
                   (strategy, label, start_d, end_d, params, summary)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                   RETURNING id""",
                (strategy, label, start_d, end_d,
                 json.dumps(params, default=str),
                 json.dumps(summary, default=str)),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if trades:
        db.executemany(
            """INSERT INTO backtest_trades
               (run_id, code, entry_d, exit_d, entry_px, exit_px, ret_pct, exit_reason)
               VALUES %s""",
            [(run_id, t["code"], t["entry_d"], t["exit_d"],
              t["entry_px"], t["exit_px"], t["ret_pct"], t["exit_reason"])
             for t in trades],
        )

    print(f"  백테스트 저장: run_id={run_id}  매매 {len(trades):,}건")
    return run_id
