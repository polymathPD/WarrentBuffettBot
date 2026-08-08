"""
신용융자 잔고 수집 → credit_balance
pykrx에 신용잔고 API가 없으므로 KIS OpenAPI 사용 (계좌 개설 후 활성화)
그 전까지는 stock_daily 거래대금 기반 임시 추정치로 채움
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date, timedelta
import db.connection as db
import config

SOURCE = "credit_balance"


def collect_stub(start_date: str = "20220101", end_date: str = None):
    """
    KIS API 준비 전 임시: stock_daily의 거래대금을 기반으로
    dummy credit_balance를 0으로 채워 스키마를 유지함.
    실제 신용잔고 데이터는 KIS API 연동 후 collect_kis()로 교체.
    """
    today = date.today()
    end = end_date or today.strftime("%Y%m%d")

    rows_to_insert = db.fetchall(
        """SELECT DISTINCT sd.code, sd.d
           FROM stock_daily sd
           LEFT JOIN credit_balance cb ON cb.code = sd.code AND cb.d = sd.d
           WHERE cb.code IS NULL
           AND sd.d >= %s::date AND sd.d <= %s::date
           LIMIT 50000""",
        (start_date[:4] + "-" + start_date[4:6] + "-" + start_date[6:],
         end[:4] + "-" + end[4:6] + "-" + end[6:]),
    )

    if not rows_to_insert:
        print("신용잔고 stub: 새로운 행 없음")
        return

    rows = [(r["code"], r["d"], 0, 0.0) for r in rows_to_insert]
    db.executemany(
        """INSERT INTO credit_balance (code, d, credit_amt, credit_ratio)
           VALUES %s ON CONFLICT (code, d) DO NOTHING""",
        rows,
    )
    print(f"신용잔고 stub {len(rows)}건 삽입 (0값, KIS API 연동 전)")


def collect_kis():
    """KIS OpenAPI로 실제 신용잔고 수집 (계좌 개설 후 구현)"""
    if not config.KIS_APP_KEY:
        print("KIS API 키 미설정 — collect_stub()으로 대체")
        collect_stub()
        return
    # TODO: KIS API /uapi/domestic-stock/v1/quotations/credit-by-date
    raise NotImplementedError("KIS API 연동 후 구현")


if __name__ == "__main__":
    collect_stub()
