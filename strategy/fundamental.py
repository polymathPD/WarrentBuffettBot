"""
펀더멘털 전략 규칙 (역발상 전략과 독립 병행)

- 진입: 분기·반기·사업보고서가 공시된 날, 그 보고서 기준으로
        (1) 영업이익이 전년 동기보다 개선되고 흑자
        (2) ROE가 기준 이상, 부채비율이 기준 이하
        인 종목을 개선율 순으로 고른다. 체결은 공시일 다음 거래일 시가.
- 청산: 손절 → 만기 → (최소 보유기간 이후) 이익 훼손.
        최소 보유 5거래일은 손절에만 예외를 둔다.

손익 항목은 사업연도 누적치라 같은 분기끼리(2026Q2 vs 2025Q2) 비교해야 한다.
db/schema.sql의 financials 주석 참고.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import re
from datetime import date
import db.connection as db
import config

STRATEGY = "fundamental_v1"

REPORT_PREFIXES = ("분기보고서", "반기보고서", "사업보고서")
PERIOD_RE = re.compile(r"\((\d{4})\.(\d{2})\)")
MONTH_TO_QUARTER = {"03": 1, "06": 2, "09": 3, "12": 4}

ROE_MIN = 0.03        # 누적 순이익 / 기말 자본
DEBT_MAX = 2.0        # 부채총계 / 자본총계
MIN_HOLD_DAYS = 5     # 거래일 기준. 손절은 예외
CANDIDATE_LIMIT = 50


def _period_of(report_nm: str) -> str | None:
    """'반기보고서 (2026.06)' -> '2026Q2'. 결산월이 다르면 None."""
    m = PERIOD_RE.search(report_nm or "")
    if not m:
        return None
    quarter = MONTH_TO_QUARTER.get(m.group(2))
    return f"{m.group(1)}Q{quarter}" if quarter else None


def _prev_year(period: str) -> str:
    year, quarter = period.split("Q")
    return f"{int(year) - 1}Q{quarter}"


def _passes(cur: dict, prev: dict) -> tuple[bool, float]:
    """진입 조건 판정과 영업이익 개선율(랭킹용)."""
    if not cur or not prev:
        return False, 0.0

    op, prev_op = cur.get("op_income"), prev.get("op_income")
    equity, net, debt = cur.get("equity"), cur.get("net_income"), cur.get("liabilities")
    if op is None or prev_op is None or equity is None or net is None or debt is None:
        return False, 0.0

    op, prev_op = float(op), float(prev_op)
    equity, net, debt = float(equity), float(net), float(debt)
    if equity <= 0 or op <= 0 or op <= prev_op:
        return False, 0.0
    if net / equity < ROE_MIN or debt / equity > DEBT_MAX:
        return False, 0.0

    # 적자에서 흑자로 돌아선 경우 분모가 0에 가까워 개선율이 폭주하므로 자본으로 잰다.
    improvement = (op - prev_op) / abs(prev_op) if prev_op > 0 else (op - prev_op) / equity
    return True, improvement


def get_entry_candidates(target_date: str = None) -> list[dict]:
    """
    target_date에 실적 보고서를 낸 종목 중 조건을 통과한 후보.
    반환은 개선율 내림차순.
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    filings = db.fetchall(
        """SELECT code, report_nm FROM disclosures
           WHERE d = %s::date
             AND (report_nm LIKE '분기보고서%%'
               OR report_nm LIKE '반기보고서%%'
               OR report_nm LIKE '사업보고서%%')""",
        (d,),
    )
    if not filings:
        return []

    held = {
        r["code"] for r in db.fetchall(
            "SELECT code FROM positions WHERE strategy=%s", (STRATEGY,)
        )
    }

    # 종목마다 (당기, 전년 동기) 두 기간이 필요하다. 한 번에 받아 파이썬에서 맞춘다.
    wanted = {}
    for f in filings:
        period = _period_of(f["report_nm"])
        if period and f["code"] not in held:
            wanted[f["code"]] = period
    if not wanted:
        return []

    periods = set(wanted.values()) | {_prev_year(p) for p in wanted.values()}
    fins = db.fetchall(
        """SELECT code, period, op_income, net_income, equity, liabilities
           FROM financials WHERE code = ANY(%s) AND period = ANY(%s)""",
        (list(wanted), list(periods)),
    )
    by_key = {(r["code"], r["period"]): r for r in fins}

    prices = {
        r["code"]: float(r["c"]) for r in db.fetchall(
            "SELECT code, c FROM stock_daily WHERE d = %s::date AND code = ANY(%s)",
            (d, list(wanted)),
        )
    }

    candidates = []
    for code, period in wanted.items():
        if code not in prices:
            continue
        ok, improvement = _passes(
            by_key.get((code, period)), by_key.get((code, _prev_year(period)))
        )
        if ok:
            candidates.append({
                "code": code,
                "close": prices[code],
                "period": period,
                "improvement": improvement,
            })

    candidates.sort(key=lambda c: -c["improvement"])
    return candidates[:CANDIDATE_LIMIT]


def get_exit_candidates(target_date: str = None) -> list[dict]:
    """
    청산 후보. 손절이 최우선이고, 최소 보유기간을 채우기 전에는 만기 청산을 하지 않는다.
    보유기간은 거래일 기준으로 센다(달력일이 아니라 stock_daily 봉 수).
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    rows = db.fetchall(
        """SELECT p.code, p.name, p.entry_px, p.qty, p.stop_px, p.max_hold_days, p.mode,
                  sd.c AS close_price,
                  (SELECT COUNT(*) FROM stock_daily h
                    WHERE h.code = p.code AND h.d > p.entry_date AND h.d <= %s::date) AS held_days
           FROM positions p
           JOIN stock_daily sd ON sd.code = p.code AND sd.d = %s::date
           WHERE p.strategy = %s""",
        (d, d, STRATEGY),
    )

    exits = []
    for r in rows:
        close = float(r["close_price"])
        held_days = int(r["held_days"])
        reason = None

        if close <= float(r["stop_px"]):
            reason = "stop"
        elif held_days >= MIN_HOLD_DAYS and held_days >= r["max_hold_days"]:
            reason = "expiry"

        if reason:
            exits.append({
                "code": r["code"],
                "name": r["name"],
                "close": close,
                "entry_px": float(r["entry_px"]),
                "qty": float(r["qty"]),
                "reason": reason,
                "mode": r["mode"],
            })

    return exits
