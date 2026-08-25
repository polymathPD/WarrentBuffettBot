"""
일별 자산 스냅샷을 equity_daily에 기록한다.

  현금       = CAPITAL - 매수금액 + 매도금액
  평가금액   = 보유수량 × 기준일 종가 (기준일 봉이 없으면 직전 거래일 종가)
  총자산     = 현금 + 평가금액

전략마다 자본금을 따로 굴리는 것으로 본다. 기본값은 전역 CAPITAL이지만
settings에 CAPITAL_<전략명>이 있으면 그 값을 쓴다.

전략을 갈아탈 때 새 전략이 또 전역 CAPITAL로 시작하면 이전 전략의 손익이
기록에서 사라진다. 2026-08-21에 실제로 그랬다 — 역발상이 9,955,186원으로
끝났는데 퀄리티가 10,000,000원을 새로 받아 총자산이 2,000만원으로 찍혔다.
갈아탈 때는 이전 전략의 최종 자산을 새 전략의 CAPITAL_<전략명>으로 넘긴다.

은퇴한 전략(RETIRED_STRATEGIES)은 자산 집계에서 뺀다. 자금을 넘겼으므로
그 전략에는 더 이상 돈이 없다. 과거 기록은 그대로 남는다.

positions는 현재 상태만 갖고 있으므로 과거 날짜를 소급 기록할 수는 없다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date
import db.connection as db
import config


def _overrides() -> tuple[dict, set]:
    """전략별 자본금과 은퇴 목록. settings에 없으면 빈 값."""
    caps, retired = {}, set()
    for r in db.fetchall(
        "SELECT key, value FROM settings "
        "WHERE key LIKE 'CAPITAL\_%%' OR key = 'RETIRED_STRATEGIES'"
    ):
        if r["key"] == "RETIRED_STRATEGIES":
            retired = {x.strip() for x in (r["value"] or "").split(",") if x.strip()}
        else:
            try:
                caps[r["key"][len("CAPITAL_"):]] = float(r["value"])
            except (TypeError, ValueError):
                pass
    return caps, retired


def capital_for(strategy: str) -> float:
    """그 전략이 굴리는 자본금."""
    caps, _ = _overrides()
    return caps.get(strategy, float(config.get_setting("CAPITAL")))


def cash_by_key(target_date: str = None) -> dict:
    """(mode, strategy) -> 현금. 기준일까지의 매수금액을 빼고 매도금액을 더한다.
    은퇴한 전략은 자금을 넘겼으므로 제외한다."""
    d = target_date or date.today().strftime("%Y-%m-%d")
    caps, retired = _overrides()
    default_cap = float(config.get_setting("CAPITAL"))

    flows = db.fetchall(
        """SELECT mode, strategy,
                  SUM(CASE WHEN side='buy' THEN -amount ELSE amount END) AS flow
           FROM trades WHERE ts::date <= %s::date
           GROUP BY mode, strategy""",
        (d,),
    )
    return {(r["mode"], r["strategy"]):
            caps.get(r["strategy"], default_cap) + float(r["flow"] or 0)
            for r in flows if r["strategy"] not in retired}


def snapshot(target_date: str = None) -> int:
    """기준일의 (mode, strategy)별 자산을 기록하고 기록한 행 수를 반환한다.

    live 모드는 KIS가 계산한 순자산(nass_amt)을 그대로 저장한다. 우리 장부로
    현금을 재계산하면 매수 재실행으로 trades가 부풀려진 경우 왜곡되고, KIS
    모의는 dnca_tot_amt가 매수 뒤에도 그대로라 dnca + scts로 총자산을 구하면
    T+2 결제분이 빠져 실제보다 부풀려진다. 브로커 계산값이 유일한 진실이다.
    """
    d = target_date or date.today().strftime("%Y-%m-%d")
    _, retired = _overrides()

    cash = cash_by_key(d)
    values = db.fetchall(
        """SELECT p.mode, p.strategy, SUM(p.qty * sd.c) AS v
           FROM positions p
           JOIN LATERAL (
               SELECT c FROM stock_daily
               WHERE code = p.code AND d <= %s::date
               ORDER BY d DESC LIMIT 1
           ) sd ON TRUE
           WHERE p.mode <> 'live'
           GROUP BY p.mode, p.strategy""",
        (d,),
    )

    held = {(r["mode"], r["strategy"]): float(r["v"] or 0) for r in values}

    rows = []
    for key in cash.keys() | held.keys():
        mode, strategy = key
        if strategy in retired or mode == "live":
            continue
        c = cash.get(key, capital_for(strategy))
        v = held.get(key, 0.0)
        rows.append((d, mode, strategy, c, v, c + v, None))

    # live 모드는 KIS 스냅샷으로 별도 처리. 조회 실패 시 그 전략만 건너뛴다.
    #
    # cash는 dnca_tot_amt(예수금)이 아니라 nass_amt - scts_evlu_amt(실질 현금)이다.
    # KIS 모의는 매수해도 dnca가 안 줄어들어 화면상 예수금이 그대로 남는데, 이걸
    # '쓸 수 있는 현금'으로 보여 주면 미수 상태를 못 알아본다 — 오늘 대시보드에
    # 현금 10M로 찍혀 있어서 -16.4M 미수가 숨겨졌다. 음수라도 실체를 그대로 낸다.
    live_strategies = {s for (m, s) in cash.keys() if m == "live" and s not in retired}
    if live_strategies:
        try:
            from executor.live import account_snapshot
            snap = account_snapshot()
            settled_cash = snap["total_equity"] - snap["positions_value"]
            for strategy in live_strategies:
                rows.append((d, "live", strategy, settled_cash,
                             snap["positions_value"], snap["total_equity"],
                             snap["unrealized"]))
        except Exception as e:
            print(f"[자산] live 스냅샷 실패 - 건너뜀: {type(e).__name__} {str(e)[:120]}")

    if rows:
        db.executemany(
            """INSERT INTO equity_daily (d, mode, strategy, cash, positions_value,
                                        total_equity, unrealized)
               VALUES %s
               ON CONFLICT (d, mode, strategy) DO UPDATE
               SET cash = EXCLUDED.cash,
                   positions_value = EXCLUDED.positions_value,
                   total_equity = EXCLUDED.total_equity,
                   unrealized = EXCLUDED.unrealized""",
            rows,
        )

    for _, mode, strategy, c, v, total, _u in rows:
        print(f"[자산] {d} {mode}/{strategy}  현금 {c:,.0f}  평가 {v:,.0f}  총 {total:,.0f}")
    return len(rows)


if __name__ == "__main__":
    snapshot(sys.argv[1] if len(sys.argv) > 1 else None)
