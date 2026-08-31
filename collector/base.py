"""수집기 공통 뼈대.

수집기는 전부 스케줄러(daily_job)에서만 불린다. 공통 계약은 `Collector.run()`
하나이고, 생성자에서 수집 범위를 받는다.

KIS 일별 시계열 두 개(investor_flow, credit_balance)는 API와 컬럼만 다르고
페이지네이션·증분 판정·커서 기록이 같아서 `KisDailyCollector`가 그 흐름을
전부 갖는다. 하위 클래스는 요청 파라미터와 행 변환만 채운다.

DART 세 개(disclosure, financials, shares)는 조회 단위가 서로 달라(기간 / 100종목
배치 / 종목 1건) 흐름을 공유하지 않는다. 대신 재시도와 status 판정이 같으므로
`dart_get()`을 함께 쓴다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from abc import ABC, abstractmethod
from datetime import date
import time

import requests

import config
import db.connection as db

# DART 공통 응답 코드
DART_OK = "000"
DART_NO_DATA = "013"      # 조회된 데이터가 없습니다 (오류가 아님)
DART_RATE_LIMIT = "020"   # 요청 제한 초과 (일일 한도)


class RateLimited(RuntimeError):
    """DART 일일 호출 한도 초과. 재시도해도 소용없으니 즉시 멈춘다."""


class Collector(ABC):
    """모든 수집기의 공통 계약.

    SOURCE는 collect_cursor의 source 키다. 커서를 종목별로 쓰지 않는 수집기도
    있지만(공시는 '*' 한 행), 어떤 수집기가 남긴 커서인지는 이 값으로 구분한다.
    """

    SOURCE: str = ""

    @abstractmethod
    def run(self) -> None:
        """수집을 수행한다. 스케줄러가 부르는 유일한 진입점이다."""


def dart_get(url: str, params: dict, timeout: int,
             max_retries: int = 3, retry_wait_sec: float = 1.0) -> dict:
    """DART API 한 번 호출. 일시 오류만 재시도한다.

    status 020(일일 한도)은 RateLimited로 올린다 — 재시도가 한도를 더 먹는다.
    013(데이터 없음)은 오류가 아니라 정상 응답이라 그대로 돌려준다.
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status == DART_RATE_LIMIT:
                raise RateLimited(data.get("message") or "DART 일일 한도 초과")
            if status not in (DART_OK, DART_NO_DATA):
                raise RuntimeError(f"DART 오류 {status}: {data.get('message')}")
            return data
        except RateLimited:
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_wait_sec * attempt)
    raise last_err


class KisDailyCollector(Collector):
    """KIS 일별 시계열을 종목마다 과거방향으로 훑어 적재한다.

    한 번에 최대 30건만 오므로, 앵커 날짜를 최과거일로 당겨가며 start_bound에
    닿을 때까지 반복한다. 완주했을 때만 커서를 남긴다 — 페이지 한도로 끊겼는데
    커서를 찍으면 다음 실행의 start_bound가 올라가 못 받은 구간이 영구 결손된다.
    """

    TABLE: str = ""          # 증분 판정에 쓰는 대상 테이블
    LABEL: str = ""          # 로그에 찍는 이름
    API_URL: str = ""
    TR_ID: str = ""
    OUTPUT_KEY: str = "output"
    DATE_FIELD: str = ""     # 응답에서 날짜를 담은 필드명
    INSERT_SQL: str = ""

    # KIS 모의투자 계정은 초당 2건 한도다. 한도를 넘으면 HTTP 500 + msg1
    # "초당 거래건수를 초과하였습니다"로 돌아온다(429가 아니다).
    # 실측: sleep 0.3s면 12건 중 2건이 이 오류였고, 0.6s면 0건이었다.
    # 재시도가 다시 한도를 먹어 실패가 실패를 부르므로 간격을 넉넉히 둔다.
    SLEEP_SEC = 0.6
    MAX_RETRIES = 3          # 페이지 단위 재시도 (KIS 간헐적 5xx 대응)
    RETRY_WAIT_SEC = 1.0     # 재시도 대기 (시도마다 선형 증가)
    # 한 페이지당 최대 30건이지만 다음 페이지가 앵커 날짜를 다시 포함하므로
    # 실효 진행은 페이지당 약 29거래일. start_bound에 도달하면 루프가 먼저
    # 끊기므로 이 값을 올려도 정상 케이스의 호출 수는 늘지 않는다(약 14년치).
    MAX_PAGES_PER_CODE = 120

    def __init__(self, start_date: str = "20220101", end_date: str = None):
        self.start_date = start_date
        self.end_date = end_date

    # --- 하위 클래스가 채우는 부분 ---------------------------------------

    @abstractmethod
    def params(self, code: str, anchor_date: str) -> dict:
        """조회 요청 파라미터."""

    @abstractmethod
    def values(self, row: dict) -> tuple:
        """응답 한 건에서 (code, d) 뒤에 붙일 값들."""

    def missing_keys(self) -> bool:
        """API 키가 없어 수집이 불가능한지. 하위 클래스가 대체 동작을 넣을 수 있다."""
        return not (config.KIS_APP_KEY and config.KIS_APP_SECRET)

    def on_missing_keys(self) -> None:
        print(f"KIS API 키 미설정 -> {self.SOURCE} 수집 불가")

    # --- 공통 흐름 -------------------------------------------------------

    def latest_available_date(self) -> str:
        """수집 종료 앵커(YYYYMMDD). 일봉이 존재하는 마지막 날짜를 쓴다.

        KIS는 당일 데이터를 장 마감 이후에야 제공하므로, 그 전에 오늘 날짜로
        조회하면 전 종목이 시간 제한으로 거부된다. 일봉조차 없는 날짜를 요청할
        이유가 없으니 stock_daily의 최신일을 앵커로 삼는다. 스케줄러 흐름에서는
        일봉 수집이 먼저 끝나므로 자연스럽게 당일이 된다.
        """
        row = db.fetchone("SELECT MAX(d) AS d FROM stock_daily")
        if row and row["d"]:
            return row["d"].strftime("%Y%m%d")
        return date.today().strftime("%Y%m%d")

    def last_data_date(self, code: str):
        """이 종목의 실제 데이터 최신일. 없으면 None.

        증분 기준은 '언제 수집했는가'(collect_cursor.last_seen)가 아니라
        '어디까지 받았는가'여야 한다. 커서는 Postgres NOW()(UTC)라, 전 종목
        수집이 자정을 넘겨 끝나면 그 시각이 다음 날짜로 읽혀 다음 실행이 전
        종목을 건너뛴다. 시간대 해석이 끼어들지 않도록 데이터 자체를 본다.
        (커서는 universe.target_codes()가 수집 이력을 유지하는 데 계속 쓰인다.)
        """
        row = db.fetchone(
            f"SELECT MAX(d) AS d FROM {self.TABLE} WHERE code=%s", (code,))
        return row["d"] if row and row["d"] else None

    def headers(self) -> dict:
        from executor.live import _get_token
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {_get_token()}",
            "appKey": config.KIS_APP_KEY,
            "appSecret": config.KIS_APP_SECRET,
            "tr_id": self.TR_ID,
            "custtype": "P",
        }

    def fetch_page(self, code: str, anchor_date: str) -> list[dict]:
        """anchor_date(YYYYMMDD) 기준 최대 30건을 과거방향으로 조회.

        KIS 서버가 간헐적으로 500을 반환한다(실측 요청당 약 10%). 한 종목이
        수십 페이지를 순차 조회하므로 페이지 하나가 실패해 종목 전체가 중단되면
        종목 단위 실패율이 급격히 올라간다(요청당 2%만 잡아도 39페이지면 54%).
        따라서 일시적 오류(네트워크·5xx)는 페이지 단위로 재시도하고, rt_cd 업무
        오류는 재시도하지 않는다(재조회해도 같은 결과).
        """
        from executor.live import _BASE_URL
        params = self.params(code, anchor_date)
        for attempt in range(self.MAX_RETRIES):
            last = attempt == self.MAX_RETRIES - 1
            try:
                resp = requests.get(f"{_BASE_URL}{self.API_URL}",
                                    headers=self.headers(), params=params, timeout=10)
            except requests.RequestException:
                if last:
                    raise
                time.sleep(self.RETRY_WAIT_SEC * (attempt + 1))
                continue

            if resp.status_code >= 500 and not last:
                # 초당 한도는 잠깐 더 쉬어야 풀린다. 일반 5xx와 같은 간격으로
                # 재시도하면 재시도가 다시 한도를 먹는다.
                wait = self.RETRY_WAIT_SEC * (attempt + 1)
                if "초당" in resp.text:
                    wait = max(wait, self.SLEEP_SEC * 3)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") not in (None, "0"):
                raise RuntimeError(f"{self.LABEL} 조회 실패: {data.get('msg1')}")
            return data.get(self.OUTPUT_KEY, [])

    def _sweep_code(self, code: str, start_bound: str, end: str) -> tuple[bool, bool]:
        """한 종목을 start_bound까지 훑는다. (뭔가 넣었나, 완주했나)를 돌려준다."""
        anchor = end
        collected_any = False
        for _ in range(self.MAX_PAGES_PER_CODE):
            rows = self.fetch_page(code, anchor)
            if not rows:
                # 더 줄 데이터가 없음(신규상장/상장폐지 등) -> 완주로 간주
                return collected_any, True

            to_insert = []
            oldest_d = None
            for r in rows:
                d_str = r.get(self.DATE_FIELD)
                if not d_str or len(d_str) != 8:
                    continue
                d = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
                if oldest_d is None or d < oldest_d:
                    oldest_d = d
                if d.strftime("%Y%m%d") < start_bound:
                    continue
                to_insert.append((code, d) + self.values(r))

            if to_insert:
                db.executemany(self.INSERT_SQL, to_insert)
                collected_any = True

            next_anchor = oldest_d.strftime("%Y%m%d") if oldest_d else None
            if next_anchor is None or next_anchor <= start_bound:
                return collected_any, True
            if next_anchor >= anchor:
                # 앵커가 과거로 밀리지 않음 = 더 줄 데이터가 없음.
                # 이 가드가 없으면 같은 요청을 MAX_PAGES_PER_CODE만큼 반복한다.
                return collected_any, True
            anchor = next_anchor
            time.sleep(self.SLEEP_SEC)

        return collected_any, False

    def run(self) -> None:
        if self.missing_keys():
            self.on_missing_keys()
            return

        from collector.universe import target_codes

        end = self.end_date or self.latest_available_date()
        tickers = target_codes(self.SOURCE)
        total = len(tickers)
        errors = []

        print(f"{self.LABEL} 수집 대상: {total}종목 (KIS API)")

        for i, code in enumerate(tickers, 1):
            try:
                last = self.last_data_date(code)
                if last and last.strftime("%Y%m%d") >= end:
                    continue
                start_bound = last.strftime("%Y%m%d") if last else self.start_date
                if start_bound > end:
                    continue

                collected_any, reached_start = self._sweep_code(code, start_bound, end)

                # 커서는 start_bound까지 완주했을 때만 기록한다.
                if collected_any and reached_start:
                    db.execute(
                        """INSERT INTO collect_cursor (source, code, last_seen)
                           VALUES (%s, %s, NOW())
                           ON CONFLICT (source, code) DO UPDATE SET last_seen = NOW()""",
                        (self.SOURCE, code),
                    )
                elif not reached_start:
                    errors.append((code, f"페이지 한도({self.MAX_PAGES_PER_CODE}) 소진 — "
                                         f"{start_bound}까지 미도달, 커서 미기록"
                                         f"(다음 실행에서 재시도)"))

            except Exception as e:
                errors.append((code, str(e)))

            if i % 100 == 0 or i == total:
                print(f"  [{i}/{total}] 완료  오류: {len(errors)}")

            time.sleep(self.SLEEP_SEC)

        if errors:
            print(f"\n오류 목록 ({len(errors)}건, 상위 20개):")
            for code, msg in errors[:20]:
                print(f"  {code}: {msg}")

        print(f"{self.LABEL} 수집 완료 (KIS API)")
