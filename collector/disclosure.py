"""
DART 공시 목록 수집 → disclosures
DART 공시검색 API (https://opendart.fss.or.kr/api/list.json)

종목별로 조회하면 3,900번 호출해야 하므로, 기간으로 한 번에 받아 우리 종목만
남긴다. 응답에 stock_code가 들어 있어 종목 매핑도 그대로 쓸 수 있다.
기간이 길면 페이지 수가 커지므로 월 단위로 잘라 호출한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

import time
from datetime import date, timedelta
import requests
import db.connection as db
import config

SOURCE = "disclosure"
API_URL = "https://opendart.fss.or.kr/api/list.json"
CORP_CLASSES = ("Y", "K")     # 유가증권 / 코스닥 (비상장·기타 제외)
PAGE_COUNT = 100              # API 최대치
MAX_PAGES = 300               # 월/시장 조합당 안전장치
SLEEP_SEC = 0.2
MAX_RETRIES = 3
RETRY_WAIT_SEC = 1.0
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

NO_DATA = "013"               # 조회된 데이터가 없습니다 (오류가 아님)


def _month_ranges(start: date, end: date):
    """[start, end]를 달력 월 경계로 잘라 (bgn, end) 순으로 내놓는다."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def fetch_page(bgn: date, end: date, corp_cls: str, page_no: int) -> dict:
    """공시검색 한 페이지. 재시도해도 실패하면 예외를 올린다."""
    params = {
        "crtfc_key": config.DART_API_KEY,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "corp_cls": corp_cls,
        "page_no": page_no,
        "page_count": PAGE_COUNT,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") not in ("000", NO_DATA):
                raise RuntimeError(f"DART 오류 {data.get('status')}: {data.get('message')}")
            return data
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SEC * attempt)
    raise last_err


def _rows(data: dict, known: set) -> list[tuple]:
    """응답 목록에서 우리가 보는 종목만 골라 INSERT 행으로 만든다."""
    out = []
    for item in data.get("list", []):
        code = (item.get("stock_code") or "").strip()
        rcept_no = (item.get("rcept_no") or "").strip()
        if code not in known or not rcept_no:
            continue
        d = item["rcept_dt"]
        out.append((
            rcept_no,
            code,
            f"{d[:4]}-{d[4:6]}-{d[6:8]}",
            (item.get("report_nm") or "").strip(),
            VIEWER_URL + rcept_no,
        ))
    return out


def _last_cursor() -> date | None:
    row = db.fetchone(
        "SELECT last_seen FROM collect_cursor WHERE source=%s AND code=%s",
        (SOURCE, "*"),
    )
    return row["last_seen"].date() if row and row["last_seen"] else None


def collect(start_date: str = None, end_date: str = None) -> int:
    """
    [start_date, end_date] 구간의 공시를 수집한다.
    start_date를 안 주면 마지막 수집일부터(없으면 오늘) 이어서 받는다.
    한 건이라도 실패하면 커서를 올리지 않아 다음 실행이 같은 구간을 다시 받는다.
    """
    if not config.DART_API_KEY:
        print("DART_API_KEY 미설정: 공시 수집을 건너뜁니다")
        return 0

    end = date.fromisoformat(end_date) if end_date else date.today()
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        cursor = _last_cursor()
        start = cursor if cursor else end
    if start > end:
        print(f"수집할 구간 없음 (start={start} > end={end})")
        return 0

    known = {r["code"] for r in db.fetchall("SELECT code FROM instruments")}
    saved = failed = 0

    for bgn, fin in _month_ranges(start, end):
        for corp_cls in CORP_CLASSES:
            page_no = 1
            while page_no <= MAX_PAGES:
                try:
                    data = fetch_page(bgn, fin, corp_cls, page_no)
                except Exception as e:
                    print(f"  [실패] {bgn}~{fin} {corp_cls} p{page_no}: {e}")
                    failed += 1
                    break

                if data.get("status") == NO_DATA:
                    break

                rows = _rows(data, known)
                if rows:
                    db.executemany(
                        """INSERT INTO disclosures (rcept_no, code, d, report_nm, url)
                           VALUES %s ON CONFLICT (rcept_no) DO UPDATE
                           SET report_nm = EXCLUDED.report_nm""",
                        rows,
                    )
                    saved += len(rows)

                if page_no >= int(data.get("total_page") or 1):
                    break
                page_no += 1
                time.sleep(SLEEP_SEC)

        print(f"  {bgn}~{fin} 누적 {saved:,}건")

    if failed:
        print(f"공시 수집 {saved:,}건 저장, 실패 {failed}건: 커서를 올리지 않습니다")
    else:
        db.execute(
            """INSERT INTO collect_cursor (source, code, last_seen)
               VALUES (%s, %s, %s) ON CONFLICT (source, code)
               DO UPDATE SET last_seen = EXCLUDED.last_seen""",
            (SOURCE, "*", end),
        )
        print(f"공시 수집 완료: {saved:,}건 저장 (커서 {end})")
    return saved


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else None
    e = sys.argv[2] if len(sys.argv) > 2 else None
    collect(s, e)
