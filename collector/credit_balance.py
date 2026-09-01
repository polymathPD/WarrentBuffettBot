"""
신용융자 잔고 수집 → credit_balance
KIS OpenAPI [국내주식-110] 신용잔고 일별추이
(/uapi/domestic-stock/v1/quotations/daily-credit-balance, tr_id=FHPST04760000)

페이지네이션·증분 판정·커서 기록은 collector/base.py의 KisDailyCollector가 갖는다.
KIS API 키가 없으면 stub으로 스키마만 유지한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

from datetime import date

import db.connection as db
from collector.base import KisDailyCollector


class CreditBalanceCollector(KisDailyCollector):
    SOURCE = "credit_balance"
    TABLE = "credit_balance"
    LABEL = "신용잔고"
    API_URL = "/uapi/domestic-stock/v1/quotations/daily-credit-balance"
    TR_ID = "FHPST04760000"
    OUTPUT_KEY = "output"
    DATE_FIELD = "stlm_date"     # 결제일자
    INSERT_SQL = """INSERT INTO credit_balance (code, d, credit_amt, credit_ratio)
                    VALUES %s ON CONFLICT (code, d) DO NOTHING"""

    def params(self, code: str, anchor_date: str) -> dict:
        return {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20476",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": anchor_date,
        }

    def values(self, row: dict) -> tuple:
        return (int(row.get("whol_loan_rmnd_amt") or 0),
                float(row.get("whol_loan_rmnd_rate") or 0))

    def on_missing_keys(self) -> None:
        print("KIS API 키 미설정 -> stub으로 대체")
        self.collect_stub()

    def collect_stub(self) -> None:
        """KIS API 없이 스키마만 유지한다.

        stock_daily에 있는 (종목, 날짜)에 0을 채운다. 실제 신용잔고가 아니므로
        신호 계산은 이 값을 의미 있게 쓰지 못한다 — 키를 넣으면 run()이 실제
        데이터로 덮는다.
        """
        end = self.end_date or date.today().strftime("%Y%m%d")
        start = self.start_date

        rows_to_insert = db.fetchall(
            """SELECT DISTINCT sd.code, sd.d
               FROM stock_daily sd
               LEFT JOIN credit_balance cb ON cb.code = sd.code AND cb.d = sd.d
               WHERE cb.code IS NULL
               AND sd.d >= %s::date AND sd.d <= %s::date
               LIMIT 50000""",
            (f"{start[:4]}-{start[4:6]}-{start[6:]}",
             f"{end[:4]}-{end[4:6]}-{end[6:]}"),
        )

        if not rows_to_insert:
            print("신용잔고 stub: 새로운 행 없음")
            return

        rows = [(r["code"], r["d"], 0, 0.0) for r in rows_to_insert]
        db.executemany(self.INSERT_SQL, rows)
        print(f"신용잔고 stub {len(rows)}건 삽입 (0값, KIS API 연동 전)")
