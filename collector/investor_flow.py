"""
KIS OpenAPI 종목별 투자자매매동향(일별) 수집 → investor_flow
(/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily, tr_id=FHPTJ04160001)

페이지네이션·증분 판정·커서 기록은 collector/base.py의 KisDailyCollector가 갖는다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

from collector.base import KisDailyCollector

# KIS의 *_ntby_tr_pbmn 필드 단위(백만원) -> DB 저장 단위(원)
PBMN_TO_WON = 1_000_000


class InvestorFlowCollector(KisDailyCollector):
    SOURCE = "investor_flow"
    TABLE = "investor_flow"
    LABEL = "수급"
    API_URL = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
    TR_ID = "FHPTJ04160001"
    OUTPUT_KEY = "output2"
    DATE_FIELD = "stck_bsop_date"
    # DO NOTHING이면 단위가 틀린 행을 재수집으로 고칠 수 없다.
    INSERT_SQL = """INSERT INTO investor_flow
                    (code, d, individual_net, foreign_net, institution_net)
                    VALUES %s
                    ON CONFLICT (code, d) DO UPDATE SET
                      individual_net = EXCLUDED.individual_net,
                      foreign_net = EXCLUDED.foreign_net,
                      institution_net = EXCLUDED.institution_net"""

    def params(self, code: str, anchor_date: str) -> dict:
        return {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": anchor_date,
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        }

    def values(self, row: dict) -> tuple:
        """순매수 3종. KIS는 '백만원' 단위로 주므로 원으로 환산한다.

        db/schema.sql의 investor_flow 주석 참고 — 환산이 없으면 원 단위로 이관한
        기존 행과 10^6배 어긋나고, flow_ratio의 30일 창이 두 단위를 물어
        heat_score가 전 종목 0으로 죽는다.
        """
        return (
            int(row.get("prsn_ntby_tr_pbmn") or 0) * PBMN_TO_WON,
            int(row.get("frgn_ntby_tr_pbmn") or 0) * PBMN_TO_WON,
            int(row.get("orgn_ntby_tr_pbmn") or 0) * PBMN_TO_WON,
        )
