"""신용융자 과열 에이전트"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call

AGENT = "credit_heat"


def analyze(code: str, target_date: str) -> dict:
    sig = db.fetchone(
        "SELECT credit_surge_ratio, volume_ratio, heat_score FROM contrarian_signals "
        "WHERE code=%s AND d=%s::date",
        (code, target_date),
    )
    credit_r = float(sig["credit_surge_ratio"]) if sig and sig["credit_surge_ratio"] else 0.0
    vol_r = float(sig["volume_ratio"]) if sig and sig["volume_ratio"] else 1.0
    heat = float(sig["heat_score"]) if sig else 0.0

    prompt = f"""너는 신용융자와 거래대금 과열 전문가다.

[종목] {code}  [기준일] {target_date}
[신용잔고 배율] {credit_r:.2f}배 (1.0 = 30일 평균)
[거래대금 배율] {vol_r:.2f}배 (1.0 = 30일 평균)
[과열 점수] {heat:.1f}/10

과거 통계: 신용잔고 배율 1.5 이상이면 반대매매 압력으로 이후 10일 평균 -0.8% 경향.
거래대금 배율 3.0 이상은 단기 고점 후 조정 확률 65%.

신용·거래대금 과열도를 고려해 신규 매수 여부를 판단하라.
[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
