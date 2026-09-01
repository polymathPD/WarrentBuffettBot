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

import db.connection as db
import config
from collector.base import Collector, dart_get, RateLimited
from collector.financials import REPORTS

COMMON = "보통주"


def _count(text: str):
    t = (text or "").strip().replace(",", "")
    if not t or t == "-":
        return None
    try:
        return int(t)
    except ValueError:
        return None


class SharesCollector(Collector):
    SOURCE = "shares"
    API_URL = "https://opendart.fss.or.kr/api/stockTotqySttus.json"
    SLEEP_SEC = 0.1
    BATCH_INSERT = 200

    def __init__(self, year: str = None, reprt_code: str = "11011"):
        if reprt_code not in REPORTS:
            raise ValueError(f"reprt_code는 {list(REPORTS)} 중 하나여야 합니다: {reprt_code}")
        self.year = year or str(int(time.strftime("%Y")) - 1)
        self.reprt_code = reprt_code
        self.period = f"{self.year}Q{REPORTS[reprt_code]}"

    def fetch(self, corp_code: str) -> dict:
        return dart_get(self.API_URL, {
            "crtfc_key": config.DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": self.year,
            "reprt_code": self.reprt_code,
        }, timeout=30)

    def row(self, data: dict, code: str):
        """보통주 행만 쓴다 (우선주는 종목코드가 따로고, 시총 계산은 보통주 기준)."""
        for item in data.get("list", []):
            if (item.get("se") or "").strip() != COMMON:
                continue
            issued = _count(item.get("istc_totqy"))
            if issued is None:
                return None
            return (code, self.period, issued,
                    _count(item.get("tesstk_co")), _count(item.get("distb_stock_co")))
        return None

    def save(self, rows):
        db.executemany(
            """INSERT INTO shares (code, period, issued, treasury, floating)
               VALUES %s ON CONFLICT (code, period) DO UPDATE
               SET issued = EXCLUDED.issued, treasury = EXCLUDED.treasury,
                   floating = EXCLUDED.floating""",
            rows,
        )

    def run(self) -> int:
        """(year, reprt_code) 기간의 발행주식수를 아직 없는 종목에 대해 수집한다."""
        if not config.DART_API_KEY:
            print("DART_API_KEY 미설정: 발행주식수 수집을 건너뜁니다")
            return 0

        done = {r["code"] for r in db.fetchall(
            "SELECT code FROM shares WHERE period = %s", (self.period,))}
        targets = [r for r in db.fetchall(
            "SELECT code, dart_corp_code FROM instruments "
            "WHERE dart_corp_code IS NOT NULL ORDER BY code")
            if r["code"] not in done]

        print(f"{self.period}: 대상 {len(targets):,}종목 (이미 수집 {len(done):,}종목)")
        rows, saved, missing = [], 0, 0

        for i, r in enumerate(targets, 1):
            try:
                data = self.fetch(r["dart_corp_code"])
            except RateLimited as e:
                print(f"  [중단] DART 일일 한도 초과: {e}")
                print(f"  {saved:,}종목까지 저장됨. 내일 다시 실행하면 이어받습니다")
                break
            except Exception as e:
                print(f"  [실패] {r['code']}: {e}")
                missing += 1
                continue

            row = self.row(data, r["code"])
            if row:
                rows.append(row)
            else:
                missing += 1

            if len(rows) >= self.BATCH_INSERT:
                self.save(rows)
                saved += len(rows)
                rows = []
                print(f"  {i:,}/{len(targets):,}  저장 {saved:,}종목", flush=True)
            time.sleep(self.SLEEP_SEC)

        if rows:
            self.save(rows)
            saved += len(rows)

        print(f"{self.period}: {saved:,}종목 저장 (보고서 없음·실패 {missing:,}종목)")
        return saved
