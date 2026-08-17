"""
일별 자산 스냅샷을 equity_daily에 기록한다.

  현금       = CAPITAL - 매수금액 + 매도금액
  평가금액   = 보유수량 × 기준일 종가 (기준일 봉이 없으면 직전 거래일 종가)
  총자산     = 현금 + 평가금액

(mode, strategy) 조합마다 CAPITAL을 따로 굴리는 것으로 본다.
positions는 현재 상태만 갖고 있으므로 과거 날짜를 소급 기록할 수는 없다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date
import db.connection as db
import config


def snapshot(target_date: str = None) -> int:
    """기준일의 (mode, strategy)별 자산을 기록하고 기록한 행 수를 반환한다."""
    d = target_date or date.today().strftime("%Y-%m-%d")
    capital = float(config.get_setting("CAPITAL"))

    flows = db.fetchall(
        """SELECT mode, strategy,
                  SUM(CASE WHEN side='buy' THEN -amount ELSE amount END) AS flow
           FROM trades WHERE ts::date <= %s::date
           GROUP BY mode, strategy""",
        (d,),
    )
    values = db.fetchall(
        """SELECT p.mode, p.strategy, SUM(p.qty * sd.c) AS v
           FROM positions p
           JOIN LATERAL (
               SELECT c FROM stock_daily
               WHERE code = p.code AND d <= %s::date
               ORDER BY d DESC LIMIT 1
           ) sd ON TRUE
           GROUP BY p.mode, p.strategy""",
        (d,),
    )

    cash = {(r["mode"], r["strategy"]): capital + float(r["flow"] or 0) for r in flows}
    held = {(r["mode"], r["strategy"]): float(r["v"] or 0) for r in values}

    rows = []
    for key in cash.keys() | held.keys():
        mode, strategy = key
        c = cash.get(key, capital)
        v = held.get(key, 0.0)
        rows.append((d, mode, strategy, c, v, c + v))

    if rows:
        db.executemany(
            """INSERT INTO equity_daily (d, mode, strategy, cash, positions_value, total_equity)
               VALUES %s
               ON CONFLICT (d, mode, strategy) DO UPDATE
               SET cash = EXCLUDED.cash,
                   positions_value = EXCLUDED.positions_value,
                   total_equity = EXCLUDED.total_equity""",
            rows,
        )

    for _, mode, strategy, c, v, total in rows:
        print(f"[자산] {d} {mode}/{strategy}  현금 {c:,.0f}  평가 {v:,.0f}  총 {total:,.0f}")
    return len(rows)


if __name__ == "__main__":
    snapshot(sys.argv[1] if len(sys.argv) > 1 else None)
