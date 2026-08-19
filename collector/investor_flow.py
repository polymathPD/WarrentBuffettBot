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
from collector.universe import target_codes
from executor.live import _get_token, _BASE_URL

SOURCE = "investor_flow"

# KIS의 *_ntby_tr_pbmn 필드 단위(백만원) -> DB 저장 단위(원)
PBMN_TO_WON = 1_000_000
API_URL = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
TR_ID = "FHPTJ04160001"
# KIS 모의투자 계정은 초당 2건 한도다. 한도를 넘으면 HTTP 500 + msg1
# "초당 거래건수를 초과하였습니다"로 돌아온다(429가 아니다).
# 실측: sleep 0.3s면 12건 중 2건이 이 오류였고, 0.6s면 0건이었다.
# 재시도가 다시 한도를 먹어 실패가 실패를 부르므로 간격을 넉넉히 둔다.
SLEEP_SEC = 0.6
MAX_RETRIES = 3       # 페이지 단위 재시도 (KIS 간헐적 5xx 대응)
RETRY_WAIT_SEC = 1.0  # 재시도 대기 (시도마다 선형 증가)
# 한 페이지당 최대 30건이지만 다음 페이지가 앵커 날짜를 다시 포함하므로
# 실효 진행은 페이지당 약 29거래일. 2022-01-01 기준으로는 39페이지면 충분하지만,
# 수집 기간이 길어져도 조용히 잘리지 않도록 여유를 크게 둔다(약 14년치).
# start_bound에 도달하면 루프가 먼저 끊기므로 이 값을 올려도 호출 수는 늘지 않는다.
MAX_PAGES_PER_CODE = 120


def _latest_available_date() -> str:
    """수집 종료 앵커(YYYYMMDD). 일봉이 존재하는 마지막 날짜를 쓴다.

    KIS 투자자매매동향은 당일 데이터를 15:40 이후에야 제공하므로, 그 전에 오늘
    날짜로 조회하면 전 종목이 "TIME LIMIT 00:00 ~ 15:40"으로 거부된다. 일봉조차
    없는 날짜의 수급을 요청할 이유가 없으니 stock_daily의 최신일을 앵커로 삼는다.
    스케줄러 흐름에서는 일봉 수집이 먼저 끝나므로 자연스럽게 당일이 된다.
    """
    row = db.fetchone("SELECT MAX(d) AS d FROM stock_daily")
    if row and row["d"]:
        return row["d"].strftime("%Y%m%d")
    return date.today().strftime("%Y%m%d")


def _last_data_date(code: str):
    """이 종목의 실제 데이터 최신일. 없으면 None.

    증분 수집의 기준은 '언제 수집했는가'(collect_cursor.last_seen)가 아니라
    '어디까지 받았는가'여야 한다. 커서는 Postgres NOW()(UTC)라, 전 종목 수집이
    자정을 넘겨 끝나면 그 시각이 다음 날짜로 읽혀 다음 실행이 전 종목을 건너뛴다.
    실제로 수집이 13시간 걸린 날 하루치가 통째로 비었다. 시간대 해석이 끼어들지
    않도록 데이터 자체를 본다. (커서는 universe.target_codes()가 수집 이력을
    유지하는 데 계속 쓰이므로 기록은 그대로 남긴다.)
    """
    row = db.fetchone("SELECT MAX(d) AS d FROM investor_flow WHERE code=%s", (code,))
    return row["d"] if row and row["d"] else None


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
    """anchor_date(YYYYMMDD) 기준 최대 30건을 과거방향으로 조회.

    KIS 서버가 간헐적으로 500을 반환한다(실측 요청당 약 10%). 한 종목이 수십
    페이지를 순차 조회하므로 페이지 하나가 실패해 종목 전체가 중단되면 종목 단위
    실패율이 급격히 올라간다(요청당 2%만 잡아도 39페이지면 54%). 따라서 일시적
    오류(네트워크·5xx)는 페이지 단위로 재시도하고, rt_cd 업무 오류는 재시도하지
    않는다(재조회해도 같은 결과).
    """
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": anchor_date,
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
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
            # 초당 한도는 잠깐 더 쉬어야 풀린다. 일반 5xx와 같은 간격으로 재시도하면
            # 재시도가 다시 한도를 먹는다.
            wait = RETRY_WAIT_SEC * (attempt + 1)
            if "초당" in resp.text:
                wait = max(wait, SLEEP_SEC * 3)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"투자자매매동향 조회 실패: {data.get('msg1')}")
        return data.get("output2", [])


def collect(start_date: str = "20220101", end_date: str = None):
    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        print("KIS API 키 미설정 -> investor_flow 수집 불가")
        return

    end = end_date or _latest_available_date()
    tickers = target_codes(SOURCE)
    total = len(tickers)
    errors = []

    print(f"수급 수집 대상: {total}종목 (KIS API)")

    for i, code in enumerate(tickers, 1):
        try:
            last = _last_data_date(code)
            if last and last.strftime("%Y%m%d") >= end:
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
                    # API가 더 줄 데이터가 없음(신규상장/상장폐지 등) -> 완주로 간주
                    reached_start = True
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
                    # KIS의 *_ntby_tr_pbmn은 '백만원' 단위다. DB는 원 단위로 통일해
                    # 저장하므로 여기서 환산한다 (db/schema.sql의 investor_flow 주석 참고).
                    # 과거에 이 환산이 없어 참조 DB에서 이관한 원 단위 데이터와 10^6배
                    # 어긋났고, heat_score가 전 종목 0으로 죽었다.
                    individual = int(r.get("prsn_ntby_tr_pbmn") or 0) * PBMN_TO_WON
                    foreign = int(r.get("frgn_ntby_tr_pbmn") or 0) * PBMN_TO_WON
                    institution = int(r.get("orgn_ntby_tr_pbmn") or 0) * PBMN_TO_WON
                    to_insert.append((code, d, individual, foreign, institution))

                if to_insert:
                    db.executemany(
                        """INSERT INTO investor_flow
                           (code, d, individual_net, foreign_net, institution_net)
                           VALUES %s
                           ON CONFLICT (code, d) DO UPDATE SET
                             individual_net = EXCLUDED.individual_net,
                             foreign_net = EXCLUDED.foreign_net,
                             institution_net = EXCLUDED.institution_net""",
                        to_insert,
                    )
                    collected_any = True

                next_anchor = oldest_d.strftime("%Y%m%d") if oldest_d else None
                if next_anchor is None or next_anchor <= start_bound:
                    reached_start = True
                    break
                if next_anchor >= anchor:
                    # 앵커가 과거로 밀리지 않음 = 더 줄 데이터가 없음.
                    # 이 가드가 없으면 같은 요청을 MAX_PAGES_PER_CODE만큼 반복한다.
                    reached_start = True
                    break
                anchor = next_anchor
                time.sleep(SLEEP_SEC)

            # 커서는 start_bound까지 완주했을 때만 기록한다.
            # 페이지 한도 소진으로 끝났는데 커서를 찍으면 다음 실행의 start_bound가
            # 그 날짜로 올라가 버려서, 못 받은 과거 구간이 영구히 결손으로 남는다.
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

    print("수급 수집 완료 (KIS API)")


if __name__ == "__main__":
    collect()
