"""
KIS OpenAPI 종목별 투자자매매동향(일별) 수집 → investor_flow
(/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily, tr_id=FHPTJ04160001)

과거에는 pykrx로 data.krx.co.kr을 스크래핑했으나, 대량 자동 조회가
KRX 이용약관(제10조 제2호) 위반으로 탐지되어 IP가 1일간 차단된 바 있음.
KRX 공식 Open API(openapi.krx.co.kr)는 투자자별 매매동향 자체를 제공하지
않아, 이미 검증된 KIS OpenAPI 계정으로 전환함 (executor/live.py 토큰 재사용,
credit_balance.py와 동일한 페이지네이션 패턴).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

from datetime import date
import time
import requests
import db.connection as db
import config
from executor.live import _get_token, _BASE_URL

SOURCE = "investor_flow"
API_URL = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
TR_ID = "FHPTJ04160001"
SLEEP_SEC = 0.3
MAX_PAGES_PER_CODE = 40  # 한 페이지당 최대 30건 -> 약 3년치 커버


def _last_collected(code: str):
    row = db.fetchone(
        "SELECT last_seen FROM collect_cursor WHERE source=%s AND code=%s",
        (SOURCE, code),
    )
    return row["last_seen"].date() if row else None


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_token()}",
        "appKey": config.KIS_APP_KEY,
        "appSecret": config.KIS_APP_SECRET,
        "tr_id": TR_ID,
        "custtype": "P",
    }


def _fetch_page(code: str, anchor_date: str) -> list[dict]:
    """anchor_date(YYYYMMDD) 기준 최대 30건을 과거방향으로 조회"""
    resp = requests.get(
        f"{_BASE_URL}{API_URL}",
        headers=_headers(),
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": anchor_date,
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") not in (None, "0"):
        raise RuntimeError(f"투자자매매동향 조회 실패: {data.get('msg1')}")
    return data.get("output2", [])


def collect(start_date: str = "20220101", end_date: str = None):
    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        print("KIS API 키 미설정 -> investor_flow 수집 불가")
        return

    today = date.today()
    end = end_date or today.strftime("%Y%m%d")
    tickers = db.fetchall("SELECT DISTINCT code FROM stock_daily")
    tickers = [r["code"] for r in tickers]
    total = len(tickers)
    errors = []

    print(f"수급 수집 대상: {total}종목 (KIS API)")

    for i, code in enumerate(tickers, 1):
        try:
            last = _last_collected(code)
            if last and last >= today:
                continue
            start_bound = last.strftime("%Y%m%d") if last else start_date
            if start_bound > end:
                continue

            anchor = end
            collected_any = False
            for _ in range(MAX_PAGES_PER_CODE):
                rows = _fetch_page(code, anchor)
                if not rows:
                    break

                to_insert = []
                oldest_d = None
                for r in rows:
                    d_str = r.get("stck_bsop_date")
                    if not d_str or len(d_str) != 8:
                        continue
                    d = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
                    if oldest_d is None or d < oldest_d:
                        oldest_d = d
                    if d.strftime("%Y%m%d") < start_bound:
                        continue
                    individual = int(r.get("prsn_ntby_tr_pbmn") or 0)
                    foreign = int(r.get("frgn_ntby_tr_pbmn") or 0)
                    institution = int(r.get("orgn_ntby_tr_pbmn") or 0)
                    to_insert.append((code, d, individual, foreign, institution))

                if to_insert:
                    db.executemany(
                        """INSERT INTO investor_flow
                           (code, d, individual_net, foreign_net, institution_net)
                           VALUES %s ON CONFLICT (code, d) DO NOTHING""",
                        to_insert,
                    )
                    collected_any = True

                if oldest_d is None or oldest_d.strftime("%Y%m%d") <= start_bound:
                    break
                anchor = oldest_d.strftime("%Y%m%d")
                time.sleep(SLEEP_SEC)

            if collected_any:
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
        print(f"\n오류 목록 ({len(errors)}건, 상위 20개):")
        for code, msg in errors[:20]:
            print(f"  {code}: {msg}")

    print("수급 수집 완료 (KIS API)")


if __name__ == "__main__":
    collect()
