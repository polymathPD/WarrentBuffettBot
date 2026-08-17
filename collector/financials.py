"""
DART 주요계정 재무 수집 → financials
DART 다중회사 주요계정 API (https://opendart.fss.or.kr/api/fnlttMultiAcnt.json)

한 번에 100종목까지 조회된다(120종목은 status 021로 거부). 전 종목이 40여 회
호출로 끝나므로 시가총액으로 걸러 호출을 아낄 이유가 없어, 고유번호가 매핑된
종목을 모두 받는다. 종목 선별은 전략 쪽에서 한다.

EPS/BPS는 이 API에 없다. 주요계정은 재무상태표·손익계산서의 합계 계정만 준다.

손익 항목은 사업연도 누적치로 온다(2026 반기 = 2026.01~06 누적). 분기 단독
실적이 필요하면 직전 분기를 빼야 한다 — db/schema.sql의 financials 주석 참고.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

import time
from datetime import date
import requests
import db.connection as db
import config

SOURCE = "financials"
API_URL = "https://opendart.fss.or.kr/api/fnlttMultiAcnt.json"
BATCH = 100            # API 상한
SLEEP_SEC = 0.2
MAX_RETRIES = 3
RETRY_WAIT_SEC = 1.0

NO_DATA = "013"        # 조회된 데이터가 없습니다 (오류가 아님)

# 보고서 코드 -> 분기 표기
REPORTS = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}

# 계정명은 제출사마다 표기가 갈린다. 공백을 지운 뒤 아래 별칭으로 맞춘다.
ACCOUNTS = {
    "revenue": ("매출액", "수익(매출액)", "영업수익"),
    "op_income": ("영업이익", "영업이익(손실)"),
    "net_income": ("당기순이익", "당기순이익(손실)"),
    "assets": ("자산총계",),
    "liabilities": ("부채총계",),
    "equity": ("자본총계",),
}
FIELDS = tuple(ACCOUNTS)


def _amount(text: str):
    """'44,425,929,000,000' -> int. 빈 값·'-'는 None."""
    t = (text or "").strip().replace(",", "")
    if not t or t == "-":
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = int(float(t))
    except ValueError:
        return None
    return -v if neg else v


def fetch_batch(corp_codes: list[str], year: str, reprt_code: str) -> dict:
    """주요계정 한 배치. 재시도해도 실패하면 예외를 올린다."""
    params = {
        "crtfc_key": config.DART_API_KEY,
        "corp_code": ",".join(corp_codes),
        "bsns_year": year,
        "reprt_code": reprt_code,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=60)
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


def _rows(data: dict, period: str, by_corp: dict) -> list[tuple]:
    """
    응답을 (종목, 재무제표구분)별로 모아 INSERT 행으로 만든다.
    연결(CFS)이 있으면 연결을, 없으면 개별(OFS)을 쓴다.
    """
    picked: dict = {}
    for item in data.get("list", []):
        code = by_corp.get(item.get("corp_code"))
        if not code:
            continue
        name = (item.get("account_nm") or "").replace(" ", "")
        field = next((f for f, aliases in ACCOUNTS.items() if name in aliases), None)
        if field is None:
            continue
        acc = picked.setdefault((code, item.get("fs_div")), {})
        acc[field] = _amount(item.get("thstrm_amount"))

    rows = []
    for code in {c for c, _ in picked}:
        fs_div = "CFS" if (code, "CFS") in picked else "OFS"
        acc = picked.get((code, fs_div))
        if not acc:
            continue
        rows.append((code, period, fs_div) + tuple(acc.get(f) for f in FIELDS))
    return rows


def collect(year: str = None, reprt_code: str = "11011") -> int:
    """
    (year, reprt_code) 한 기간의 주요계정을 전 종목에 대해 수집한다.
    reprt_code: 11013=1분기 11012=반기 11014=3분기 11011=사업보고서
    """
    if not config.DART_API_KEY:
        print("DART_API_KEY 미설정: 재무 수집을 건너뜁니다")
        return 0
    if reprt_code not in REPORTS:
        raise ValueError(f"reprt_code는 {list(REPORTS)} 중 하나여야 합니다: {reprt_code}")

    year = year or str(date.today().year)
    period = f"{year}Q{REPORTS[reprt_code]}"

    mapped = db.fetchall(
        "SELECT code, dart_corp_code FROM instruments "
        "WHERE dart_corp_code IS NOT NULL ORDER BY code"
    )
    if not mapped:
        print("dart_corp_code 매핑 없음: collector/dart_corp_code.py를 먼저 실행하세요")
        return 0

    by_corp = {r["dart_corp_code"]: r["code"] for r in mapped}
    corp_codes = [r["dart_corp_code"] for r in mapped]
    saved = failed = 0

    for i in range(0, len(corp_codes), BATCH):
        batch = corp_codes[i:i + BATCH]
        try:
            data = fetch_batch(batch, year, reprt_code)
        except Exception as e:
            print(f"  [실패] 배치 {i // BATCH + 1}: {e}")
            failed += 1
            continue

        rows = _rows(data, period, by_corp)
        if rows:
            db.executemany(
                """INSERT INTO financials
                   (code, period, fs_div, revenue, op_income, net_income,
                    assets, liabilities, equity)
                   VALUES %s ON CONFLICT (code, period) DO UPDATE
                   SET fs_div = EXCLUDED.fs_div,
                       revenue = EXCLUDED.revenue,
                       op_income = EXCLUDED.op_income,
                       net_income = EXCLUDED.net_income,
                       assets = EXCLUDED.assets,
                       liabilities = EXCLUDED.liabilities,
                       equity = EXCLUDED.equity""",
                rows,
            )
            saved += len(rows)
        time.sleep(SLEEP_SEC)

    print(f"{period}: {saved:,}종목 저장 "
          f"({len(corp_codes):,}종목 요청, 실패 배치 {failed})")

    if not failed:
        db.execute(
            """INSERT INTO collect_cursor (source, code, last_seen)
               VALUES (%s, %s, NOW()) ON CONFLICT (source, code)
               DO UPDATE SET last_seen = NOW()""",
            (SOURCE, period),
        )
    return saved


if __name__ == "__main__":
    y = sys.argv[1] if len(sys.argv) > 1 else None
    r = sys.argv[2] if len(sys.argv) > 2 else "11011"
    collect(y, r)
