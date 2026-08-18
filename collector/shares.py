"""
발행주식수 수집 → shares
DART 주식의총수현황 API (https://opendart.fss.or.kr/api/stockTotqySttus.json)

다중회사 조회가 없어 종목당 1콜이다. 발행주식수는 증자·분할이 없으면 잘 안 바뀌므로
사업보고서(연 1회) 기준으로 받는 것을 기본으로 한다.

이미 저장된 (종목, 기간)은 건너뛰므로 중간에 끊겨도 다시 실행하면 이어받는다.
DART 일일 호출 한도(2만)에 걸리면 status 020이 오는데, 그때는 즉시 멈춘다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

import time
import requests
import db.connection as db
import config
from collector.financials import REPORTS

API_URL = "https://opendart.fss.or.kr/api/stockTotqySttus.json"
SLEEP_SEC = 0.1
MAX_RETRIES = 3
RETRY_WAIT_SEC = 1.0
BATCH_INSERT = 200

NO_DATA = "013"
RATE_LIMIT = "020"    # 요청 제한 초과 (일일 한도)
COMMON = "보통주"


class RateLimited(RuntimeError):
    pass


def _count(text: str):
    t = (text or "").strip().replace(",", "")
    if not t or t == "-":
        return None
    try:
        return int(t)
    except ValueError:
        return None


def fetch(corp_code: str, year: str, reprt_code: str) -> dict:
    params = {"crtfc_key": config.DART_API_KEY, "corp_code": corp_code,
              "bsns_year": year, "reprt_code": reprt_code}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status == RATE_LIMIT:
                raise RateLimited(data.get("message") or "DART 일일 한도 초과")
            if status not in ("000", NO_DATA):
                raise RuntimeError(f"DART 오류 {status}: {data.get('message')}")
            return data
        except RateLimited:
            raise
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SEC * attempt)
    raise last_err


def _row(data: dict, code: str, period: str):
    """보통주 행만 쓴다 (우선주는 종목코드가 따로고, 시총 계산은 보통주 기준)."""
    for item in data.get("list", []):
        if (item.get("se") or "").strip() != COMMON:
            continue
        issued = _count(item.get("istc_totqy"))
        if issued is None:
            return None
        return (code, period, issued,
                _count(item.get("tesstk_co")), _count(item.get("distb_stock_co")))
    return None


def _save(rows):
    db.executemany(
        """INSERT INTO shares (code, period, issued, treasury, floating)
           VALUES %s ON CONFLICT (code, period) DO UPDATE
           SET issued = EXCLUDED.issued, treasury = EXCLUDED.treasury,
               floating = EXCLUDED.floating""",
        rows,
    )


def collect(year: str = None, reprt_code: str = "11011") -> int:
    """(year, reprt_code) 기간의 발행주식수를 아직 없는 종목에 대해 수집한다."""
    if not config.DART_API_KEY:
        print("DART_API_KEY 미설정: 발행주식수 수집을 건너뜁니다")
        return 0
    if reprt_code not in REPORTS:
        raise ValueError(f"reprt_code는 {list(REPORTS)} 중 하나여야 합니다: {reprt_code}")

    year = year or str(int(time.strftime("%Y")) - 1)
    period = f"{year}Q{REPORTS[reprt_code]}"

    done = {r["code"] for r in db.fetchall(
        "SELECT code FROM shares WHERE period = %s", (period,))}
    targets = [r for r in db.fetchall(
        "SELECT code, dart_corp_code FROM instruments "
        "WHERE dart_corp_code IS NOT NULL ORDER BY code")
        if r["code"] not in done]

    print(f"{period}: 대상 {len(targets):,}종목 (이미 수집 {len(done):,}종목)")
    rows, saved, missing = [], 0, 0

    for i, r in enumerate(targets, 1):
        try:
            data = fetch(r["dart_corp_code"], year, reprt_code)
        except RateLimited as e:
            print(f"  [중단] DART 일일 한도 초과: {e}")
            print(f"  {saved:,}종목까지 저장됨. 내일 다시 실행하면 이어받습니다")
            break
        except Exception as e:
            print(f"  [실패] {r['code']}: {e}")
            missing += 1
            continue

        row = _row(data, r["code"], period)
        if row:
            rows.append(row)
        else:
            missing += 1

        if len(rows) >= BATCH_INSERT:
            _save(rows)
            saved += len(rows)
            rows = []
            print(f"  {i:,}/{len(targets):,}  저장 {saved:,}종목", flush=True)
        time.sleep(SLEEP_SEC)

    if rows:
        _save(rows)
        saved += len(rows)

    print(f"{period}: {saved:,}종목 저장 (보고서 없음·실패 {missing:,}종목)")
    return saved


if __name__ == "__main__":
    y = sys.argv[1] if len(sys.argv) > 1 else None
    rc = sys.argv[2] if len(sys.argv) > 2 else "11011"
    collect(y, rc)
