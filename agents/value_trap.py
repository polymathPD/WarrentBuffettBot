"""
가치 함정 판별 에이전트 (역발상 전략, 거부권 없음 — 합의 대상)

역발상 전략의 진입 조건은 "52주 하위 30% + 저과열 + 개인 안 몰림"이다. 이건
'떨어졌고 아무도 관심 없는 종목'의 정의이고, 그 안에는 사이클 하단(사야 함)과
구조적 훼손(사면 안 됨)이 섞여 있다. 가격·수급 숫자로는 둘이 똑같이 생겼다.
구별하는 정보는 공시 텍스트뿐이라 여기서만 LLM을 쓴다.

에이전트에는 후보 필터가 쓰지 않은 정보만 준다. heat_score와 그 구성 지표
(individual_flow_ratio / credit_surge_ratio / volume_ratio)는 넘기지 않는다 —
필터가 이미 쓴 지표를 다시 물으면 판단이 아니라 동어반복이 된다
(research/README.md "LLM 게이트가 실제로는 아무 결정도 바꾸지 않았다" 참고).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from agents.base import call

AGENT = "value_trap"

LOOKBACK_DAYS = 90
MAX_ITEMS = 25


def _price_context(code: str, target_date: str) -> dict:
    """하락의 깊이와 기간. 공시를 읽을 배경일 뿐 판단 축이 아니다."""
    row = db.fetchone(
        """SELECT c AS last_c,
                  (SELECT c FROM stock_daily WHERE code = %s AND d <= %s::date
                    ORDER BY d DESC OFFSET 59 LIMIT 1) AS c60,
                  (SELECT c FROM stock_daily WHERE code = %s AND d <= %s::date
                    ORDER BY d DESC OFFSET 249 LIMIT 1) AS c250
           FROM stock_daily WHERE code = %s AND d <= %s::date
           ORDER BY d DESC LIMIT 1""",
        (code, target_date, code, target_date, code, target_date),
    )
    if not row or row["last_c"] is None:
        return {}
    last = float(row["last_c"])
    out = {}
    if row["c60"]:
        out["r60"] = last / float(row["c60"]) - 1
    if row["c250"]:
        out["r250"] = last / float(row["c250"]) - 1
    return out


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
    px = _price_context(code, target_date)
    r60 = f"{px['r60']*100:+.1f}%" if "r60" in px else "데이터 없음"
    r250 = f"{px['r250']*100:+.1f}%" if "r250" in px else "데이터 없음"

    prompt = f"""너는 공시를 읽고 기업의 하락 원인을 판별하는 전문가다.

[종목] {code}  [기준일] {target_date}
[최근 60거래일 수익률] {r60}
[최근 250거래일 수익률] {r250}

[최근 {LOOKBACK_DAYS}일 공시] 총 {total}건 중 최신 {len(rows)}건
{listing}

이 종목은 52주 최저 부근까지 떨어져서 역발상 매수 후보에 올라왔다.
판단할 것은 단 하나다 — 이 하락이 어느 쪽인가?

(A) 업황·시장 사이클에 따른 일시적 조정 → 회복을 기대할 수 있다
(B) 기업 자체의 구조적 훼손 → 더 떨어질 수 있다. 싼 데는 이유가 있다

(B)의 근거가 되는 공시 조합을 본다. 개별 항목이 아니라 조합과 빈도를 보라:
- 자금 조달 압박: 유상증자, 전환사채·신주인수권부사채, 증권신고서(지분증권)가
  짧은 기간에 반복
- 재무 악화: 파생상품거래손실, 채무보증 급증, 담보제공, 자산·타법인주식 처분
- 지배구조 불안: 최대주주 변경, 대주주 지분 매각·담보, 대표이사 잦은 변경
- 제재·사고: 관리종목 지정, 불성실공시법인, 감사의견 비적정, 횡령·배임, 소송

반대로 자기주식 취득, 배당 확대, 대규모 수주·공급계약은 (A) 쪽 근거다.
정기보고서·소유상황보고서·IR 개최 같은 일상 공시는 근거가 아니다.

판단 규칙:
- (B)라고 볼 근거가 공시에 있으면 '관망'
- (A)이거나, 판단할 근거가 공시에 없으면 '매수'
  — 근거 없음은 무죄다. '확실하지 않아서' 관망하지 마라.

[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    return call(AGENT, code, prompt)
