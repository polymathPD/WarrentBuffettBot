"""
수집 대상 종목 유니버스.

시가총액 하위 종목은 두 가지 이유로 수집 대상에서 제외한다.

1. 비용 모델이 편도 슬리피지 0.2%(SLIP_BPS=20)를 가정하는데, 거래대금이 얇은
   종목에서는 성립하지 않는다. 넣고 백테스트하면 실현 불가능한 수익이 잡힌다.
2. 역발상 진입 필터(heat 낮음 + 52주 하위 30%)는 소외되고 하락한 종목을 고르는
   필터라, 비유동 소형주를 구조적으로 후보 상위에 올린다. 단순 노이즈가 아니라
   체계적 편향이다.

단, 이미 수집한 종목은 시총과 무관하게 계속 유지한다. FDR이 주는 시총은 '현재'
값이라, 과거에는 대형주였거나 이후 상장폐지된 종목이 소급 제외되면 시계열에
구멍이 생기고 백테스트에 생존 편향이 들어간다 — 하필 역발상 전략은 '얻어맞은
종목'을 사므로, 실제로 상장폐지까지 간 종목이 빠지면 결과가 실제보다 좋아진다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import FinanceDataReader as fdr
import db.connection as db

MIN_MARCAP = 300_000_000_000  # 3000억

_large_caps_cache: set | None = None


def large_caps() -> set[str]:
    """시총 MIN_MARCAP 이상인 종목 집합. FDR 호출이 느려 프로세스 수명 동안 캐시한다.

    FDR이 주는 시총은 '현재' 값이다. 과거 시점 판단에 쓰면 지금 상장돼 있는 종목만
    남아 생존 편향이 생기므로, 백테스트 경로에서는 쓰지 말 것.
    """
    global _large_caps_cache
    if _large_caps_cache is None:
        listing = fdr.StockListing("KRX")
        _large_caps_cache = {
            code
            for code, marcap in zip(listing["Code"], listing["Marcap"])
            if marcap and marcap >= MIN_MARCAP  # NaN은 비교에서 False
        }
    return _large_caps_cache


def target_codes(source: str) -> list[str]:
    """(시총 MIN_MARCAP 이상 ∪ 해당 source로 이미 수집된 종목) ∩ stock_daily 보유 종목"""
    big = large_caps()

    in_daily = {r["code"] for r in db.fetchall("SELECT DISTINCT code FROM stock_daily")}
    already = {
        r["code"]
        for r in db.fetchall(
            "SELECT code FROM collect_cursor WHERE source=%s", (source,)
        )
    }

    codes = (big | already) & in_daily
    print(
        f"수집 유니버스: {len(codes)}종목 "
        f"(시총 {MIN_MARCAP // 100_000_000:,}억 이상 {len(big & in_daily)}종목 "
        f"+ 기수집 {len(already & in_daily)}종목)"
    )
    return sorted(codes)
