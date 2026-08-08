"""
KIS OpenAPI 실전/모의 실행기
KIS_MOCK=true 이면 모의투자 서버, false 이면 실전 서버
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
import requests
import config

MODE = "live"

_MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
_LIVE_BASE = "https://openapi.koreainvestment.com:9443"

_IS_MOCK = os.environ.get("KIS_MOCK", "true").lower() == "true"
_BASE_URL = _MOCK_BASE if _IS_MOCK else _LIVE_BASE

# 토큰 캐시 (프로세스 수명 동안 재사용)
_token_cache: dict = {"access_token": "", "expires_at": 0}


def _get_token() -> str:
    if time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        raise RuntimeError("KIS_APP_KEY 또는 KIS_APP_SECRET 미설정")

    resp = requests.post(
        f"{_BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": config.KIS_APP_KEY,
            "appsecret": config.KIS_APP_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if "access_token" not in data:
        raise RuntimeError(f"토큰 발급 실패: {data}")

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 86400))
    return _token_cache["access_token"]


def _headers(tr_id: str) -> dict:
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_token()}",
        "appKey": config.KIS_APP_KEY,
        "appSecret": config.KIS_APP_SECRET,
        "tr_id": tr_id,
    }


def buy(code: str, qty: int) -> dict:
    """시장가 매수 주문"""
    tr_id = "VTTC0802U" if _IS_MOCK else "TTTC0802U"
    resp = requests.post(
        f"{_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_headers(tr_id),
        json={
            "CANO": config.KIS_ACCOUNT,
            "ACNT_PRDT_CD": config.KIS_ACCOUNT_SUFFIX,
            "PDNO": code,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def sell(code: str, qty: int) -> dict:
    """시장가 매도 주문"""
    tr_id = "VTTC0801U" if _IS_MOCK else "TTTC0801U"
    resp = requests.post(
        f"{_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_headers(tr_id),
        json={
            "CANO": config.KIS_ACCOUNT,
            "ACNT_PRDT_CD": config.KIS_ACCOUNT_SUFFIX,
            "PDNO": code,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_balance() -> dict:
    """잔고 조회"""
    tr_id = "VTTC8434R" if _IS_MOCK else "TTTC8434R"
    resp = requests.get(
        f"{_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=_headers(tr_id),
        params={
            "CANO": config.KIS_ACCOUNT,
            "ACNT_PRDT_CD": config.KIS_ACCOUNT_SUFFIX,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
