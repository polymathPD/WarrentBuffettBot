"""
pykrx로 투자자별 순매수 수집 → investor_flow
(계좌 불필요 — KRX 공개 데이터)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pykrx import stock as krx
from datetime import date, timedelta
import time
import db.connection as db

SOURCE = "investor_flow"
SLEEP_SEC = 0.4


def _last_collected(code: str):
    row = db.fetchone(
        "SELECT last_seen FROM collect_cursor WHERE source=%s AND code=%s",
        (SOURCE, code),
    )
    return row["last_seen"].date() if row else None


def collect(start_date: str = "20220101", end_date: str = None):
    today = date.today()
    end = end_date or today.strftime("%Y%m%d")
    tickers = db.fetchall("SELECT DISTINCT code FROM stock_daily")
    tickers = [r["code"] for r in tickers]
    total = len(tickers)
    errors = []

    print(f"수급 수집 대상: {total}종목")

    for i, code in enumerate(tickers, 1):
        last = _last_collected(code)
        if last:
            if last >= today:
                continue
            start = (last + timedelta(days=1)).strftime("%Y%m%d")
        else:
            start = start_date

        if start > end:
            continue

        try:
            # 투자자별 순매수 수량 (체결 기준)
            df = krx.get_market_net_purchases_of_equities_by_date(
                start, end, code
            )
            if df is None or df.empty:
                continue

            rows = []
            for d, r in df.iterrows():
                individual = int(r.get("개인", 0))
                foreign = int(r.get("외국인", 0))
                # 기관 = 기관합계 or 기타법인 제외한 합
                institution = int(r.get("기관합계", 0))
                rows.append((code, d.date(), individual, foreign, institution))

            if rows:
                db.executemany(
                    """INSERT INTO investor_flow
                       (code, d, individual_net, foreign_net, institution_net)
                       VALUES %s ON CONFLICT (code, d) DO NOTHING""",
                    rows,
                )
                db.execute(
                    """INSERT INTO collect_cursor (source, code, last_seen)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (source, code) DO UPDATE SET last_seen = NOW()""",
                    (SOURCE, code),
                )

        except Exception as e:
            errors.append((code, str(e)))

        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] 완료  오류: {len(errors)}")

        time.sleep(SLEEP_SEC)

    print("수급 수집 완료")


if __name__ == "__main__":
    collect()
