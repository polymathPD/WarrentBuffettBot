"""재무 추이 에이전트 (펀더멘털 전략)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call

AGENT = "financials"

PERIODS = 5   # 최근 5개 보고서


def _pct(num, den):
    if num is None or den in (None, 0):
        return None
    return float(num) / float(den) * 100


def _line(r) -> str:
    """한 기간을 '2026Q2  매출 171.5조  영업이익 89.5조(52.2%)  ROE 16.4%  부채비율 29.9%' 로."""
    def jo(v):
        return f"{float(v) / 1e12:,.1f}조" if v is not None else "-"

    margin = _pct(r["op_income"], r["revenue"])
    roe = _pct(r["net_income"], r["equity"])
    debt = _pct(r["liabilities"], r["equity"])
    return (f"  {r['period']}({r['fs_div']})  매출 {jo(r['revenue'])}  "
            f"영업이익 {jo(r['op_income'])}"
            f"{f'({margin:.1f}%)' if margin is not None else ''}  "
            f"ROE {f'{roe:.1f}%' if roe is not None else '-'}  "
            f"부채비율 {f'{debt:.0f}%' if debt is not None else '-'}")


def analyze(code: str, target_date: str) -> dict:
    rows = db.fetchall(
        """SELECT period, fs_div, revenue, op_income, net_income, equity, liabilities
           FROM financials WHERE code = %s ORDER BY period DESC LIMIT %s""",
        (code, PERIODS),
    )
    history = "\n".join(_line(r) for r in reversed(rows)) or "  재무 데이터 없음"

    prompt = f"""너는 재무제표 분석 전문가다.

[종목] {code}  [기준일] {target_date}
[최근 보고서]
{history}

주의: 매출·영업이익·순이익은 사업연도 누적치다. Q2는 상반기 누적, Q4는 연간이므로
직전 분기와 그대로 비교하면 안 되고, 같은 분기끼리(전년 동기) 비교해야 한다.

이 종목은 전년 동기 대비 영업이익이 개선되어 매수 후보에 올라왔다. 그 개선이
이어질 만한 것인지, 아니면 일회성이거나 재무 건전성이 나빠지는 중인지 판단하라.

[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
