"""공시 이력 에이전트 (펀더멘털 전략)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call

AGENT = "disclosure"

LOOKBACK_DAYS = 90
MAX_ITEMS = 15


def analyze(code: str, target_date: str) -> dict:
    rows = db.fetchall(
        """SELECT d, report_nm FROM disclosures
           WHERE code = %s AND d <= %s::date
             AND d > %s::date - INTERVAL '%s days'
           ORDER BY d DESC LIMIT %s""",
        (code, target_date, target_date, LOOKBACK_DAYS, MAX_ITEMS),
    )
    total = db.fetchone(
        """SELECT COUNT(*) AS n FROM disclosures
           WHERE code = %s AND d <= %s::date
             AND d > %s::date - INTERVAL '%s days'""",
        (code, target_date, target_date, LOOKBACK_DAYS),
    )["n"]

    listing = "\n".join(f"  {r['d']} {r['report_nm']}" for r in rows) or "  없음"

    prompt = f"""너는 공시 해석 전문가다.

[종목] {code}  [기준일] {target_date}
[최근 {LOOKBACK_DAYS}일 공시] 총 {total}건 중 최신 {len(rows)}건
{listing}

이 종목은 실적 개선을 근거로 매수 후보에 올라왔다. 공시 이력에 그 판단을 뒤집을
내용이 있는지 보라. 특히 다음은 부정 신호다:
- 유상증자·전환사채 발행(지분 희석), 감자
- 관리종목 지정, 상장폐지 사유 발생, 불성실공시법인 지정
- 횡령·배임, 소송, 감사의견 비적정
- 최대주주 변경, 대주주 지분 매각

반대로 자기주식 취득, 배당 확대, 대규모 수주는 긍정 신호다.

[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
