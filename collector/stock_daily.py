"""
pykrx로 KOSPI/KOSDAQ 전 종목 일봉 수집 → stock_daily 테이블
증분 수집: collect_cursor에서 마지막 수집일을 읽어 그 이후만 받음
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()  # pykrx가 모듈 로드 시점에 KRX_ID/KRX_PW로 세션을 만들므로 pykrx import 전에 실행돼야 함

from pykrx import stock as krx
import FinanceDataReader as fdr
from datetime import date, timedelta
import time
import db.connection as db

SOURCE = "stock_daily"
SLEEP_SEC = 0.35  # KRX 요청 간격


def _get_tickers() -> list[str]:
    df = fdr.StockListing('KRX')
    return df['Code'].tolist()


def _last_collected(code: str):
    row = db.fetchone(
        "SELECT last_seen FROM collect_cursor WHERE source=%s AND code=%s",
        (SOURCE, code),
    )
    return row["last_seen"].date() if row else None


def collect(start_date: str = "20220101", end_date: str = None):
    today = date.today()
    end = end_date or today.strftime("%Y%m%d")
    tickers = _get_tickers()
    total = len(tickers)
    errors = []

    print(f"수집 대상: {total}종목  기간: {start_date} ~ {end}")

    for i, code in enumerate(tickers, 1):
        try:
            last = _last_collected(code)
            if last:
                if last >= today:
                    continue
                start = (last + timedelta(days=1)).strftime("%Y%m%d")
            else:
                start = start_date

            if start > end:
                continue

            df = krx.get_market_ohlcv(start, end, code)
            if df is None or df.empty:
                continue

            rows = [
                (code, d.date(), int(r["시가"]), int(r["고가"]),
                 int(r["저가"]), int(r["종가"]), int(r["거래량"]))
                for d, r in df.iterrows()
            ]
            db.executemany(
                """INSERT INTO stock_daily (code, d, o, h, l, c, v)
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

    if errors:
        print(f"\n오류 목록 ({len(errors)}건):")
        for code, msg in errors[:20]:
            print(f"  {code}: {msg}")

    print("일봉 수집 완료")

# review : collector하위의 모듈들이 스탠드얼론으로 불리기 위해 메인문이 있어왔지만 이제는 모두 지울것
# review : collector 하위 모듈들 공통화 작업(base.py 만들고 abc 사용) 수행할것
# review : 더이상 스탠드얼론으로 사용안될거니까 그에 맞게 테스트도 수정할것
if __name__ == "__main__":
    collect()
