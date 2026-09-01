"""collector/shares.py - 보통주 선택, 한도 초과 중단, 재개. 네트워크/DB는 mock."""
import pytest

import config
from collector.base import RateLimited
from collector.shares import SharesCollector, _count


SAMPLE = {"status": "000", "list": [
    {"se": "보통주", "istc_totqy": "5,846,278,608",
     "tesstk_co": "82,086,705", "distb_stock_co": "5,764,191,903"},
    {"se": "우선주", "istc_totqy": "822,886,700",
     "tesstk_co": "-", "distb_stock_co": "802,371,203"},
    {"se": "합계", "istc_totqy": "6,669,165,308",
     "tesstk_co": "82,086,705", "distb_stock_co": "6,566,563,106"},
]}


def test_count_parsing():
    assert _count("5,846,278,608") == 5846278608
    assert _count("-") is None
    assert _count("") is None
    assert _count(None) is None


def test_row_takes_common_shares_only():
    """우선주는 종목코드가 따로고, 시총은 보통주 기준으로 잰다."""
    assert SharesCollector("2025", "11011").row(SAMPLE, "005930") == (
        "005930", "2025Q4", 5846278608, 82086705, 5764191903)


def test_row_returns_none_when_no_common_row():
    assert SharesCollector("2025", "11011").row(
        {"list": [{"se": "우선주", "istc_totqy": "1,000"}]}, "005930") is None


def test_fetch_raises_rate_limited_on_020(mocker):
    mocker.patch.object(config, "DART_API_KEY", "dummy")
    resp = mocker.MagicMock()
    resp.json.return_value = {"status": "020", "message": "요청 제한 초과"}
    mocker.patch("collector.base.requests.get", return_value=resp)

    with pytest.raises(RateLimited):
        SharesCollector("2025", "11011").fetch("00126380")


def test_collect_skips_already_stored_codes(mock_db, mocker):
    """중간에 끊겨도 다시 실행하면 남은 종목만 받는다."""
    mocker.patch.object(config, "DART_API_KEY", "dummy")
    mock_db.fetchall.side_effect = [
        [{"code": "005930"}],                                    # 이미 수집
        [{"code": "005930", "dart_corp_code": "00126380"},
         {"code": "000270", "dart_corp_code": "00106641"}],
    ]
    fetch = mocker.patch.object(SharesCollector, "fetch", return_value=SAMPLE)
    mocker.patch("collector.shares.time.sleep")

    SharesCollector("2025", "11011").run()

    assert fetch.call_count == 1
    assert fetch.call_args[0][0] == "00106641"


def test_collect_stops_on_rate_limit(mock_db, mocker):
    mocker.patch.object(config, "DART_API_KEY", "dummy")
    mock_db.fetchall.side_effect = [
        [],
        [{"code": f"{i:06d}", "dart_corp_code": f"{i:08d}"} for i in range(5)],
    ]
    fetch = mocker.patch.object(SharesCollector, "fetch",
                                side_effect=[SAMPLE, RateLimited("한도 초과")])
    mocker.patch("collector.shares.time.sleep")

    SharesCollector("2025", "11011").run()

    assert fetch.call_count == 2      # 한도 초과 즉시 중단
    assert mock_db.executemany.call_count == 1   # 그때까지 받은 건 저장


def test_collect_rejects_unknown_report_code():
    """생성자에서 걸러야 한다 — 잘못된 코드는 period 표기부터 만들 수 없다."""
    with pytest.raises(ValueError, match="reprt_code"):
        SharesCollector("2025", "99999")


# ---------- backtester/store.py 의 numpy 방어 ----------

def test_store_coerces_numpy_and_pandas_scalars():
    """numpy 스칼라/datetime64가 그대로 SQL에 가면 INSERT가 죽는다."""
    import numpy as np
    import pandas as pd
    from backtester.store import _native

    assert _native(np.float64(1.5)) == 1.5 and type(_native(np.float64(1.5))) is float
    assert _native(np.int64(7)) == 7 and type(_native(np.int64(7))) is int
    assert _native(np.datetime64("2026-08-14")).isoformat() == "2026-08-14"
    assert _native(pd.Timestamp("2026-08-14 09:30")).isoformat() == "2026-08-14"
    assert _native("stop") == "stop"
    assert _native(None) is None
