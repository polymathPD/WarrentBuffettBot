"""
KIS OpenAPI 실전/모의 실행기
KIS_MOCK=true 이면 모의투자 서버, false 이면 실전 서버
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
from datetime import date
import requests
import db.connection as db
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


def _find_holding(code: str) -> dict | None:
    """잔고 조회 결과(output1)에서 특정 종목 보유 내역 조회.
    필드명은 KIS 국내주식잔고조회(TTTC8434R) 표준 스펙 기준 — 실전 사용 전
    모의투자로 한 번 호출해 실제 응답 필드명과 일치하는지 확인할 것."""
    balance = get_balance()
    for item in balance.get("output1", []):
        if item.get("pdno") == code:
            return item
    return None


def buy_and_record(code: str, name: str, qty: int, strategy: str,
                   agents_summary: dict | None = None) -> bool:
    """실전 시장가 매수 주문 후 체결 확인하여 positions/trades에 mode='live'로 기록.
    슬롯은 전략별로 센다. 스케줄러에는 연결되어 있지 않음 — 수동 호출 전용."""
    held = db.fetchone(
        "SELECT COUNT(*) AS n FROM positions WHERE mode=%s AND strategy=%s",
        (MODE, strategy),
    )
    if int(held["n"]) >= config.get_setting("SLOTS"):
        print(f"[실전 매수 거부] {code} — 슬롯 부족")
        return False

    result = buy(code, qty)
    if result.get("rt_cd") != "0":
        print(f"[실전 매수 실패] {code} — {result.get('msg1')}")
        return False

    time.sleep(2)  # 체결 반영 대기
    holding = _find_holding(code)
    if not holding:
        print(f"[실전 매수 체결 확인 실패] {code} — 잔고에서 조회 안 됨, DB 기록 생략")
        return False

    entry_px = float(holding["pchs_avg_pric"])
    stop_px = entry_px * (1 - config.get_setting("STOP_PCT"))

    db.execute(
        """INSERT INTO positions (code, strategy, name, entry_date, entry_px, qty,
                                  stop_px, max_hold_days, mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (code, strategy) DO NOTHING""",
        (code, strategy, name, date.today(), entry_px, qty,
         stop_px, config.get_setting("MAX_HOLD_DAYS"), MODE),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, agents)
           VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (MODE, code, name, qty, entry_px, entry_px * qty,
         strategy, str(agents_summary or {}).replace("'", '"')),
    )
    print(f"[실전 매수] {code} {name}  진입가={entry_px:,.0f}  손절={stop_px:,.0f}")
    return True


def sell_and_record(code: str, name: str, qty: float, entry_px: float,
                    reason: str, strategy: str) -> None:
    """실전 시장가 매도 주문 후 positions/trades에 mode='live'로 기록.
    체결가는 주문 직전 잔고의 현재가(prpr)로 근사 기록 — 실제 체결가와 오차가
    있을 수 있으므로 정확한 정산은 KIS 앱/HTS 체결내역으로 별도 확인 권장."""
    holding = _find_holding(code)
    approx_px = float(holding["prpr"]) if holding else entry_px

    result = sell(code, int(qty))
    if result.get("rt_cd") != "0":
        print(f"[실전 매도 실패] {code} — {result.get('msg1')}")
        return

    realized_pct = approx_px / entry_px - 1

    db.execute(
        """UPDATE trades SET exit_reason=%s, realized_pct=%s
           WHERE ctid = (
               SELECT ctid FROM trades
               WHERE code=%s AND side='buy' AND mode=%s AND strategy=%s
                 AND exit_reason IS NULL
               ORDER BY ts DESC LIMIT 1
           )""",
        (reason, realized_pct, code, MODE, strategy),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, exit_reason, realized_pct)
           VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,%s,%s)""",
        (MODE, code, name, qty, approx_px, approx_px * qty, strategy, reason, realized_pct),
    )
    db.execute(
        "DELETE FROM positions WHERE code=%s AND mode=%s AND strategy=%s",
        (code, MODE, strategy),
    )
    pnl = "+" if realized_pct >= 0 else ""
    print(f"[실전 매도(근사)] {code} {name}  {pnl}{realized_pct*100:.2f}%  사유={reason}")
