"""개인투자자 수급 분석 에이전트"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call

AGENT = "retail_flow"


def analyze(code: str, target_date: str) -> dict:
    sig = db.fetchone(
        "SELECT individual_flow_ratio, heat_score FROM contrarian_signals "
        "WHERE code=%s AND d=%s::date",
        (code, target_date),
    )
    flow_r = float(sig["individual_flow_ratio"]) if sig and sig["individual_flow_ratio"] else 0.0
    heat = float(sig["heat_score"]) if sig else 0.0

    # 최근 5일 개인 순매수 추이
    recent = db.fetchall(
        "SELECT d, individual_net FROM investor_flow WHERE code=%s "
        "AND d <= %s::date ORDER BY d DESC LIMIT 5",
        (code, target_date),
    )
    trend = ", ".join(f"{r['d']}:{r['individual_net']:+,}" for r in recent)

    prompt = f"""너는 개인투자자 수급 분석 전문가다.

[종목] {code}  [기준일] {target_date}
[개인 순매수 배율] {flow_r:.2f}배 (1.0 = 평균)
[과열 점수] {heat:.1f}/10
[최근 5일 개인 순매수 추이] {trend if trend else '데이터 없음'}

과거 통계: 개인 순매수 배율이 2.0 이상인 종목은 이후 20일간 평균 대비 -1.2% 초과 하락 경향.

이 수급 상황에서 신규 매수를 진행할지 판단하라.
[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
