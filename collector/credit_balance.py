"""
신용융자 잔고 수집 → credit_balance
KIS OpenAPI [국내주식-110] 신용잔고 일별추이 사용
(/uapi/domestic-stock/v1/quotations/daily-credit-balance, tr_id=FHPST04760000)
KIS API 키 미설정 시 collect_stub()으로 대체
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

from datetime import date, timedelta
import time
import requests
import db.connection as db
import config
from collector.universe import target_codes
from executor.live import _get_token, _BASE_URL

SOURCE = "credit_balance"
API_URL = "/uapi/domestic-stock/v1/quotations/daily-credit-balance"
TR_ID = "FHPST04760000"
SLEEP_SEC = 0.3
MAX_RETRIES = 3       # 페이지 단위 재시도 (KIS 간헐적 5xx 대응)
RETRY_WAIT_SEC = 1.0  # 재시도 대기 (시도마다 선형 증가)
# 한 페이지당 최대 30건이지만 다음 페이지가 앵커 날짜를 다시 포함하므로
# 실효 진행은 페이지당 약 29거래일. start_bound에 도달하면 루프가 먼저 끊기므로
# 이 값을 올려도 정상 케이스의 호출 수는 늘지 않는다(약 14년치 여유).
MAX_PAGES_PER_CODE = 120


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


def _latest_available_date() -> str:
    """수집 종료 앵커(YYYYMMDD). 일봉이 존재하는 마지막 날짜를 쓴다.
    KIS는 당일 데이터를 장 마감 이후에야 제공하므로, 일봉이 없는 날짜를 앵커로
    잡으면 조회가 거부된다 (investor_flow와 동일 이유)."""
    row = db.fetchone("SELECT MAX(d) AS d FROM stock_daily")
    if row and row["d"]:
        return row["d"].strftime("%Y%m%d")
    return date.today().strftime("%Y%m%d")


def _last_collected(code: str):
    row = db.fetchone(
        "SELECT last_seen FROM collect_cursor WHERE source=%s AND code=%s",
        (SOURCE, code),
    )
    # last_seen은 Postgres NOW()(UTC). date.today()는 로컬(KST)이라 그대로 비교하면
    # KST 00~09시 구간에서 하루 어긋난다 — 로컬로 변환 후 비교한다.
    return row["last_seen"].astimezone().date() if row else None


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
    """anchor_date(YYYYMMDD, 결제일자) 기준 최대 30건을 과거방향으로 조회.

    일시적 오류(네트워크·5xx)만 페이지 단위로 재시도한다 (investor_flow와 동일 이유).
    """
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20476",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": anchor_date,
    }
    for attempt in range(MAX_RETRIES):
        last = attempt == MAX_RETRIES - 1
        try:
            resp = requests.get(
                f"{_BASE_URL}{API_URL}", headers=_headers(), params=params, timeout=10
            )
        except requests.RequestException:
            if last:
                raise
            time.sleep(RETRY_WAIT_SEC * (attempt + 1))
            continue

        if resp.status_code >= 500 and not last:
            time.sleep(RETRY_WAIT_SEC * (attempt + 1))
            continue

        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"신용잔고 조회 실패: {data.get('msg1')}")
        return data.get("output", [])


def collect_kis(start_date: str = "20220101", end_date: str = None):
    """
    KIS OpenAPI로 종목별 신용잔고 일별추이 수집 (증분).
    한 번 호출에 최대 30건(결제일자 기준 과거방향)만 반환되므로,
    start_date에 도달할 때까지 anchor_date를 앞으로 당겨가며 반복 조회한다.
    """
    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        print("KIS API 키 미설정 -> collect_stub()으로 대체")
        collect_stub(start_date, end_date)
        return

    today = date.today()
    end = end_date or _latest_available_date()
    tickers = target_codes(SOURCE)
    total = len(tickers)
    errors = []

    print(f"신용잔고 수집 대상: {total}종목 (KIS API)")

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
            reached_start = False  # start_bound까지 실제로 훑었는지
            for _ in range(MAX_PAGES_PER_CODE):
                rows = _fetch_page(code, anchor)
                if not rows:
                    # API가 더 줄 데이터가 없음 -> 완주로 간주
                    reached_start = True
                    break

                to_insert = []
                oldest_d = None
                for r in rows:
                    d_str = r.get("stlm_date")
                    if not d_str or len(d_str) != 8:
                        continue
                    d = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
                    if oldest_d is None or d < oldest_d:
                        oldest_d = d
                    if d.strftime("%Y%m%d") < start_bound:
                        continue
                    amt = int(r.get("whol_loan_rmnd_amt") or 0)
                    ratio = float(r.get("whol_loan_rmnd_rate") or 0)
                    to_insert.append((code, d, amt, ratio))

                if to_insert:
                    db.executemany(
                        """INSERT INTO credit_balance (code, d, credit_amt, credit_ratio)
                           VALUES %s ON CONFLICT (code, d) DO NOTHING""",
                        to_insert,
                    )
                    collected_any = True

                next_anchor = oldest_d.strftime("%Y%m%d") if oldest_d else None
                if next_anchor is None or next_anchor <= start_bound:
                    reached_start = True
                    break
                if next_anchor >= anchor:
                    # 앵커가 과거로 밀리지 않음 = 더 줄 데이터가 없음 (무한 재요청 방지)
                    reached_start = True
                    break
                anchor = next_anchor
                time.sleep(SLEEP_SEC)

            # 커서는 start_bound까지 완주했을 때만 기록한다(investor_flow와 동일 이유).
            if collected_any and reached_start:
                db.execute(
                    """INSERT INTO collect_cursor (source, code, last_seen)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (source, code) DO UPDATE SET last_seen = NOW()""",
                    (SOURCE, code),
                )
            elif not reached_start:
                errors.append((code, f"페이지 한도({MAX_PAGES_PER_CODE}) 소진 — "
                                     f"{start_bound}까지 미도달, 커서 미기록(다음 실행에서 재시도)"))

        except Exception as e:
            errors.append((code, str(e)))

        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] 완료  오류: {len(errors)}")

        time.sleep(SLEEP_SEC)

    if errors:
        print(f"\n오류 목록 ({len(errors)}건, 상위 20개):")
        for code, msg in errors[:20]:
            print(f"  {code}: {msg}")

    print("신용잔고 수집 완료 (KIS API)")


if __name__ == "__main__":
    collect_kis()
