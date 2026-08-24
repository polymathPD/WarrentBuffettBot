"""
KIS OpenAPI 실전/모의 실행기
KIS_MOCK=true 이면 모의투자 서버, false 이면 실전 서버
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import time
from datetime import date
import requests
import db.connection as db
import config
from executor.sizing import position_qty
from executor.guard import already_entered

# 모의투자 계좌와 실계좌를 같은 모드로 적으면 나중에 KIS_MOCK을 끄는 순간
# 두 계좌의 거래가 한 줄에 섞인다. 수익률도 자산곡선도 의미를 잃는다.
MODE = "live" if os.environ.get("KIS_MOCK", "true").lower() == "true" else "real"

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


def _field(item: dict, name: str, code: str):
    """잔고 응답에서 필드를 꺼낸다. 없으면 실제 키를 담아 즉시 알린다.

    필드명은 KIS 국내주식잔고조회(TTTC8434R) 스펙 기준이다. 모의계좌에 보유 종목이
    없으면 응답이 비어 있어 미리 확인할 수 없으므로, 어긋났을 때 KeyError로 죽는 대신
    무엇이 왔는지 보여준다."""
    if name not in item:
        raise RuntimeError(
            f"KIS 잔고 응답에 '{name}' 필드가 없습니다 ({code}). 실제 필드: {sorted(item)}"
        )
    return item[name]


def _find_holding(code: str) -> dict | None:
    """잔고 조회 결과(output1)에서 특정 종목 보유 내역 조회."""
    balance = get_balance()
    for item in balance.get("output1", []):
        if _field(item, "pdno", code) == code:
            return item
    return None


def _wait_for_qty_change(code: str, prev_qty: float,
                         timeout_s: float = 15.0,
                         poll_s: float = 2.0) -> tuple[dict | None, float]:
    """주문 뒤 잔고가 바뀔 때까지 폴링. 바뀌면 즉시, 아니면 timeout 마지막 값 반환.

    2초 고정 대기로는 KIS 모의 서버의 체결 반영을 자주 놓쳤다 — 10건 중 7건이
    '잔고 변화 없음'으로 찍혀 다음 리밸런싱까지 방치됐다. 잔고 조회는 계좌
    단위로 응답이 커서 폴링 간격은 KIS 모의(초당 2건) 한도에 맞춰 잡는다.
    """
    deadline = time.time() + timeout_s
    holding, qty = None, prev_qty
    while time.time() < deadline:
        time.sleep(poll_s)
        holding = _find_holding(code)
        qty = float(_field(holding, "hldg_qty", code)) if holding else 0.0
        if qty != prev_qty:
            break
    return holding, qty


def buy_and_record(code: str, name: str, strategy: str,
                   agents_summary: dict | None = None) -> bool:
    """실전 시장가 매수 주문 후 체결 확인하여 positions/trades에 mode='live'로 기록.
    슬롯은 전략별로 세고, 수량은 모의와 같은 규칙(1슬롯 = CAPITAL / SLOTS)으로 정한다.
    시장가 주문이라 체결가를 미리 알 수 없으므로 수량은 직전 종가로 계산한다.
    스케줄러에는 연결되어 있지 않음 — 수동 호출 전용."""
    dup = already_entered(code, strategy, MODE)
    if dup:
        print(f"[실전 매수 거부] {code} - {dup}")
        return False

    held = db.fetchone(
        "SELECT COUNT(*) AS n FROM positions WHERE mode=%s AND strategy=%s",
        (MODE, strategy),
    )
    if int(held["n"]) >= config.get_setting("SLOTS"):
        print(f"[실전 매수 거부] {code} — 슬롯 부족")
        return False

    last = db.fetchone(
        "SELECT c FROM stock_daily WHERE code=%s ORDER BY d DESC LIMIT 1", (code,)
    )
    if not last:
        print(f"[실전 매수 거부] {code} — 일봉 없음, 수량 계산 불가")
        return False
    qty = position_qty(float(last["c"]))
    if qty < 1:
        print(f"[실전 매수 거부] {code} — 1슬롯 금액으로 1주도 살 수 없음")
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

    entry_px = float(_field(holding, "pchs_avg_pric", code))
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
         strategy, json.dumps(agents_summary or {}, ensure_ascii=False)),
    )
    print(f"[실전 매수] {code} {name}  진입가={entry_px:,.0f}  손절={stop_px:,.0f}")
    return True


def sell_and_record(code: str, name: str, qty: float, entry_px: float,
                    reason: str, strategy: str) -> None:
    """실전 시장가 매도 주문 후 positions/trades에 mode='live'로 기록.
    체결가는 주문 직전 잔고의 현재가(prpr)로 근사 기록 — 실제 체결가와 오차가
    있을 수 있으므로 정확한 정산은 KIS 앱/HTS 체결내역으로 별도 확인 권장."""
    holding = _find_holding(code)
    approx_px = float(_field(holding, "prpr", code)) if holding else entry_px

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


# ── 안전장치 ────────────────────────────────────────────────────────────
def guard() -> None:
    """실계좌 주문을 막는다.

    KIS_MOCK=false는 진짜 돈이다. 설정 하나가 잘못 켜져서 주문이 나가는 일이
    없도록, 실전 서버로는 settings의 LIVE_ENABLED가 명시적으로 켜져 있을 때만
    보낸다. 기본값은 꺼짐이다.
    """
    if _IS_MOCK:
        return
    row = db.fetchone("SELECT value FROM settings WHERE key='LIVE_ENABLED'")
    if not row or str(row["value"]).lower() not in ("true", "1", "on"):
        raise RuntimeError(
            "실전 서버(KIS_MOCK=false)인데 LIVE_ENABLED가 꺼져 있습니다. "
            "실계좌 주문을 보내지 않습니다."
        )


def account_snapshot() -> dict:
    """증권사가 계산한 잔고. 우리가 더하고 빼지 않는다.

    직접 장부를 굴리면 어긋난다 — 2026-08-21에 기록 없이 38주가 생기고 현금이
    97만원 부풀려진 적이 있다. 잔고를 조회하면 그런 종류의 불일치가 없다.
    """
    b = get_balance()
    if b.get("rt_cd") != "0":
        raise RuntimeError(f"잔고 조회 실패: {b.get('msg1')}")
    holdings = {}
    for it in b.get("output1", []):
        qty = float(_field(it, "hldg_qty", it.get("pdno", "?")))
        if qty <= 0:
            continue
        holdings[it["pdno"]] = {
            "name": it.get("prdt_name", it["pdno"]),
            "qty": qty,
            "avg_px": float(_field(it, "pchs_avg_pric", it["pdno"])),
            "cur_px": float(_field(it, "prpr", it["pdno"])),
        }
    summary = (b.get("output2") or [{}])[0]
    cash = float(summary.get("dnca_tot_amt") or 0)
    return {"holdings": holdings, "cash": cash, "raw_summary": summary}


MAX_FILL_ATTEMPTS = 3


def adjust(code: str, name: str, target_qty: int, strategy: str,
           snapshot: dict, agents_summary: dict | None = None) -> None:
    """보유 수량을 target_qty로 맞춘다. 차이만 주문한다.

    체결가·수량은 주문 뒤 잔고에서 다시 읽는다. 주문을 넣었다고 체결된 것이
    아니고, 부분 체결이면 남는 수량만큼 다시 주문한다 — 시장가 한 번으로 목표에
    못 미치면 슬롯 크기가 계속 어긋난 채로 다음 달까지 방치된다.
    """
    guard()
    cur = snapshot["holdings"].get(code, {}).get("qty", 0.0)
    if int(target_qty - cur) == 0:
        return

    side = "buy" if target_qty > cur else "sell"
    after = None
    cur_qty = cur
    for attempt in range(1, MAX_FILL_ATTEMPTS + 1):
        remaining = int(target_qty - cur_qty)
        if remaining == 0:
            break
        result = (buy if remaining > 0 else sell)(code, abs(remaining))
        if result.get("rt_cd") != "0":
            print(f"[{MODE} {side} 실패] {code} {name} - {result.get('msg1')}")
            break
        after, new_qty = _wait_for_qty_change(code, cur_qty)
        if new_qty == cur_qty:
            # 이번 라운드에 한 주도 안 붙었다. 응답 지연이거나 시장가로 잡히지
            # 않는 상황이라 재시도해도 같을 것이니 그만.
            print(f"[{MODE} {side} 미체결] {code} {name} - 잔고 변화 없음 "
                  f"(시도 {attempt}/{MAX_FILL_ATTEMPTS})")
            break
        cur_qty = new_qty

    filled = cur_qty - cur
    if filled == 0:
        return
    px = (float(_field(after, "pchs_avg_pric", code)) if after
          else snapshot["holdings"].get(code, {}).get("cur_px", 0.0))
    new_qty = cur_qty
    delta = int(target_qty - cur)

    if new_qty > 0:
        db.execute(
            """INSERT INTO positions (code, strategy, name, entry_date, entry_px, qty,
                                      stop_px, max_hold_days, mode)
               VALUES (%s,%s,%s,%s,%s,%s,0,99999,%s)
               ON CONFLICT (code, strategy) DO UPDATE
                 SET qty = EXCLUDED.qty, entry_px = EXCLUDED.entry_px""",
            (code, strategy, name, date.today(), px, new_qty, MODE))
    else:
        db.execute("DELETE FROM positions WHERE code=%s AND strategy=%s AND mode=%s",
                   (code, strategy, MODE))

    if filled > 0:
        db.execute(
            """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, agents)
               VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (MODE, code, name, filled, px, px * filled, strategy,
             json.dumps(agents_summary or {}, ensure_ascii=False)))
    else:
        db.execute(
            """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy,
                                   exit_reason)
               VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,'rebalance')""",
            (MODE, code, name, -filled, px, px * -filled, strategy))
    print(f"[{MODE} {side}] {code} {name}  {filled:+.0f}주 @ {px:,.0f}"
          + (f"  (주문 {delta:+d}주, 부분체결)" if filled != delta else ""))
