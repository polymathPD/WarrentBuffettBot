"""리스크 관리 에이전트 (거부권 보유)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call
import config

AGENT = "risk"


def analyze(code: str, target_date: str, strategy: str) -> dict:
    # 슬롯은 전략별로 센다. 전체를 세면 다른 전략의 보유가 이 전략의 슬롯을 먹는다.
    held = db.fetchall("SELECT code FROM positions WHERE strategy=%s", (strategy,))
    held_count = len(held)
    free_slots = config.SLOTS - held_count

    # 업종 집중도 (같은 종목 앞 3자리가 업종 근사)
    sector_prefix = code[:3]
    same_sector = sum(1 for r in held if r["code"][:3] == sector_prefix)

    prompt = f"""너는 포트폴리오 리스크 관리 전문가다. 거부권을 행사할 수 있다.

[종목] {code}  [기준일] {target_date}  [전략] {strategy}
[전체 슬롯] {config.SLOTS}개  [사용 중] {held_count}개  [여유] {free_slots}개
[같은 업종군 보유 수] {same_sector}개

규칙:
- 여유 슬롯 0개이면 반드시 '관망'(거부권)
- 같은 업종 3개 이상이면 반드시 '관망'(거부권)
- 그 외 리스크 수준을 평가하라

[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
