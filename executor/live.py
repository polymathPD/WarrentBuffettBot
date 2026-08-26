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

# 증권사가 일시적으로 실패할 때 몇 번, 얼마 간격으로 다시 묻는지.
# 토큰은 분당 1회 제한이라 간격이 길어야 한다.
_RETRIES = 4
_TOKEN_BACKOFF_S = 65.0
_BALANCE_BACKOFF_S = 5.0


def _get_token() -> str:
    if time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        raise RuntimeError("KIS_APP_KEY 또는 KIS_APP_SECRET 미설정")

    # KIS는 토큰 발급을 분당 1회로 제한한다(403 EGW00133). 프로세스가 새로 뜬 직후나
    # 앞선 실행이 방금 발급받았으면 첫 시도가 막히므로 간격을 두고 다시 묻는다.
    data = None
    for i in range(_RETRIES):
        try:
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
            break
        except Exception as e:
            if i == _RETRIES - 1:
                raise
            wait = _TOKEN_BACKOFF_S * (i + 1)
            print(f"[{MODE} 토큰 발급 재시도 {i+1}/{_RETRIES}] {type(e).__name__} "
                  f"{str(e)[:70]} - {wait:.0f}초 뒤")
            time.sleep(wait)

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


def _order(tr_id: str, code: str, qty: int) -> dict:
    """주문 전송. 일시적 실패는 재시도한다.

    2026-08-25 리밸런싱이 여기서 죽었다. 매도 8건을 낸 뒤 다음 종목 주문이 500을
    받았고, 예외가 adjust()를 뚫고 open_job까지 올라가 남은 매도와 매수 5건,
    그리고 equity_daily 기록까지 통째로 날아갔다. 잔고 조회에는 재시도를 붙이면서
    주문 전송에는 안 붙인 것이 이유다 - 같은 서버이고 같은 방식으로 실패한다.

    주의: 재시도는 '전송 실패'에만 안전하다. 접수된 주문이 응답만 못 돌아온
    경우까지 다시 보내면 두 번 산다. 여기서는 HTTP 오류(5xx)와 연결 실패만
    다시 보내고, 응답이 온 경우(rt_cd != 0)는 호출자가 판단한다.
    """
    last = None
    for i in range(_RETRIES):
        try:
            return _order_once(tr_id, code, qty)
        except Exception as e:
            last = e
            if i == _RETRIES - 1:
                break
            wait = _BALANCE_BACKOFF_S * (i + 1)
            print(f"[{MODE} 주문 전송 재시도 {i+1}/{_RETRIES}] {code} "
                  f"{type(e).__name__} {str(e)[:60]} - {wait:.0f}초 뒤")
            time.sleep(wait)
    raise last


def _order_once(tr_id: str, code: str, qty: int) -> dict:
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


def buy(code: str, qty: int) -> dict:
    """시장가 매수 주문"""
    return _order("VTTC0802U" if _IS_MOCK else "TTTC0802U", code, qty)


def sell(code: str, qty: int) -> dict:
    """시장가 매도 주문"""
    return _order("VTTC0801U" if _IS_MOCK else "TTTC0801U", code, qty)


def get_balance() -> dict:
    """잔고 조회. 일시적 실패는 재시도한다.

    KIS 모의 서버는 이 엔드포인트에서 간헐적으로 500을 낸다. 2026-08-25 09:05
    리밸런싱이 그 500 한 번에 통째로 취소됐다 — 계좌가 미수 -1,638만원이었는데도
    `잔고 조회 실패 - 건너뜀`만 찍고 끝났다. 같은 요청이 몇 분 뒤에는 정상이었다.
    """
    tr_id = "VTTC8434R" if _IS_MOCK else "TTTC8434R"
    last = None
    for i in range(_RETRIES):
        try:
            return _get_balance_once(tr_id)
        except Exception as e:
            last = e
            if i == _RETRIES - 1:
                break
            wait = _BALANCE_BACKOFF_S * (i + 1)
            print(f"[{MODE} 잔고 조회 재시도 {i+1}/{_RETRIES}] {type(e).__name__} "
                  f"{str(e)[:70]} - {wait:.0f}초 뒤")
            time.sleep(wait)
    raise last


def _get_balance_once(tr_id: str) -> dict:
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


def _today_traded() -> tuple[float, float]:
    """오늘 누적 매수대금·매도대금. 주문 전후로 읽으면 그 주문의 실제 대금이 나온다.

    모의계좌에는 체결 단가를 주는 API가 없다 — 일별주문체결조회는 rt_cd=0에 빈
    응답이고, 실현손익·기간별매매손익은 '없는 서비스 코드'다. 잔고 요약의 이
    두 값만이 증권사가 계산한 실제 체결 금액이다.
    """
    b = get_balance()
    summary = (b.get("output2") or [{}])[0]
    return (float(summary.get("thdt_buy_amt") or 0),
            float(summary.get("thdt_sll_amt") or 0))


def _settled_cash() -> float:
    """지금 시점의 T+2 결제 예정 예수금. 매수는 이 범위 안에서만 낸다.

    KIS 모의는 기본 위탁증거금율(대개 40%)까지 미수를 허용한다 — 10M 계좌로
    26.7M어치를 사서 -16.4M 미수가 난 뒤에 붙인 방어선이다. output2의
    prvs_rcdl_excc_amt는 오늘까지 접수된 모든 주문을 반영한 뒤의 예상 예수금이라,
    이 값이 곧 '외상 없이 쓸 수 있는 돈'이다.
    """
    b = get_balance()
    summary = (b.get("output2") or [{}])[0]
    return float(summary.get("prvs_rcdl_excc_amt") or 0)


# 체결이 잔고에 반영되기까지 기다리는 시간. KIS 모의는 15초로는 한참 모자랐다.
_WAIT_S = 45.0

# 산출한 체결가가 현재가에서 이만큼 벗어나면 믿지 않는다. 국내 주식은 하루
# 등락이 ±30%로 제한되므로 그보다 먼 값은 체결가일 수 없다.
_PX_SANITY = 0.3


def _pending_sell_qty(code: str) -> float | None:
    """아직 체결되지 않은 매도 주문에 묶인 수량. 조회에 실패하면 None.

    모의계좌에는 주문 내역 조회가 없다(일별주문체결조회는 빈 응답, 정정취소가능주문은
    미지원). 하지만 잔고의 ord_psbl_qty는 '지금 주문 낼 수 있는 수량'이라, 매도 주문이
    걸려 있으면 hldg_qty보다 그만큼 적다. 이 차이가 곧 매도 대기 수량이다.

    2026-08-25에 이걸 몰라서 반대로 갔다. 부분 체결된 잔량이 아직 살아 있을까 봐
    재주문을 막았는데, 실제로는 묶인 수량이 0이었다 — 모의 서버는 시장가 잔량을
    체결시키지 않고 종료시킨다. 막을 대상이 없는데 막느라 슬롯 3개가 비었다.
    """
    try:
        h = _find_holding(code)
    except Exception as e:
        print(f"[{MODE} 대기수량 조회 실패] {code} - {type(e).__name__} {str(e)[:60]}")
        return None
    if h is None:
        return 0.0
    return float(_field(h, "hldg_qty", code)) - float(h.get("ord_psbl_qty") or 0)


def _wait_for_fill(code: str, expected_qty: float,
                   timeout_s: float = _WAIT_S,
                   poll_s: float = 3.0) -> tuple[dict | None, float]:
    """잔고가 expected_qty에 닿을 때까지 폴링. 닿으면 즉시, 아니면 timeout 마지막 값.

    예전 판은 수량이 '조금이라도' 바뀌면 곧바로 빠져나왔다. 시장가 주문이 15주 중
    3주만 붙은 순간에 폴링을 끝내고 남은 12주를 다시 주문했는데, 원주문의 12주는
    아직 살아 있었다 — 같은 물량을 두 번 산 것이다. 목표 수량에 닿을 때까지 기다리고,
    못 닿으면 재주문 여부는 _pending_sell_qty()가 결정한다.
    """
    deadline = time.time() + timeout_s
    holding, qty = _find_holding(code), None
    qty = float(_field(holding, "hldg_qty", code)) if holding else 0.0
    while qty != expected_qty and time.time() < deadline:
        time.sleep(poll_s)
        holding = _find_holding(code)
        qty = float(_field(holding, "hldg_qty", code)) if holding else 0.0
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
           ON CONFLICT (code, strategy, mode) DO NOTHING""",
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

    반환 값에 총자산(nass_amt), 유가증권 평가(scts_evlu_amt)를 함께 실어 준다.
    KIS 모의는 매수해도 dnca_tot_amt가 그대로라 dnca + scts로 총자산을 구하면
    실제(nass_amt)보다 훨씬 부풀려진다 (T+2 결제 대기분이 스냅샷에서 빠진다).
    자산 곡선·자산배분은 KIS가 계산한 nass_amt를 그대로 써야 정확하다.
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
            "evlu_amt": float(it.get("evlu_amt") or 0),
        }
    summary = (b.get("output2") or [{}])[0]
    cash = float(summary.get("dnca_tot_amt") or 0)
    positions_value = float(summary.get("scts_evlu_amt") or 0)
    total_equity = float(summary.get("nass_amt") or (cash + positions_value))
    settled_cash = float(summary.get("prvs_rcdl_excc_amt") or cash)
    unrealized = float(summary.get("evlu_pfls_smtl_amt") or 0)
    return {"holdings": holdings, "cash": cash,
            "positions_value": positions_value, "total_equity": total_equity,
            "settled_cash": settled_cash, "unrealized": unrealized,
            "raw_summary": summary}


def reconcile_positions(snapshot: dict, strategy: str,
                        agents_by_code: dict | None = None) -> list[str]:
    """증권사 잔고를 정답으로 삼아 positions를 맞춘다. 어긋났던 종목코드를 돌려준다.

    adjust()는 체결을 못 보면(filled == 0) 아무것도 기록하지 않고 끝난다. 주문은
    나갔고 증권사는 체결시켰는데 우리 DB에는 그 종목이 아예 없는 상태가 된다 —
    2026-08-24에 오리온홀딩스 78주와 영원무역홀딩스 11주가 그랬고, 매수 기록이
    없으니 대시보드의 진입 판단도 빈칸이었다. 주문을 다 낸 뒤 잔고를 다시 읽어
    맞춘다.
    """
    held = snapshot["holdings"]
    agents_by_code = agents_by_code or {}
    changed = []
    for code, h in held.items():
        row = db.fetchone(
            "SELECT qty FROM positions WHERE code=%s AND strategy=%s AND mode=%s",
            (code, strategy, MODE))
        if row is None or float(row["qty"]) != h["qty"]:
            changed.append(code)
        db.execute(
            """INSERT INTO positions (code, strategy, name, entry_date, entry_px, qty,
                                      stop_px, max_hold_days, mode, agents)
               VALUES (%s,%s,%s,%s,%s,%s,0,99999,%s,%s::jsonb)
               ON CONFLICT (code, strategy, mode) DO UPDATE
                 SET qty = EXCLUDED.qty, entry_px = EXCLUDED.entry_px,
                     agents = COALESCE(EXCLUDED.agents, positions.agents)""",
            (code, strategy, h["name"], date.today(), h["avg_px"], h["qty"], MODE,
             json.dumps(agents_by_code[code], ensure_ascii=False)
             if code in agents_by_code else None))
    gone = db.fetchall(
        "SELECT code FROM positions WHERE strategy=%s AND mode=%s AND NOT (code = ANY(%s))",
        (strategy, MODE, list(held)))
    if gone:
        changed += [r["code"] for r in gone]
        db.execute(
            "DELETE FROM positions WHERE strategy=%s AND mode=%s AND NOT (code = ANY(%s))",
            (strategy, MODE, list(held)))
    return changed


MAX_FILL_ATTEMPTS = 3


def adjust(code: str, name: str, target_qty: int, strategy: str,
           snapshot: dict, agents_summary: dict | None = None) -> None:
    """보유 수량을 target_qty로 맞춘다. 차이만 주문한다.

    체결가·수량은 주문 뒤 잔고에서 다시 읽는다. 주문을 넣었다고 체결된 것이 아니다.
    부족분을 다시 주문하는 것은 앞선 주문이 끝났다고 증권사가 확인해 준 경우뿐이다 —
    체결 반영이 늦은 것을 미체결로 오해하고 재주문하면 같은 물량을 두 번 산다.
    """
    guard()
    cur = snapshot["holdings"].get(code, {}).get("qty", 0.0)
    avg_before = float(snapshot["holdings"].get(code, {}).get("avg_px") or 0.0)
    if int(target_qty - cur) == 0:
        return

    side = "buy" if target_qty > cur else "sell"
    after = None
    cur_qty = cur
    traded_before = _today_traded()
    for attempt in range(1, MAX_FILL_ATTEMPTS + 1):
        remaining = int(target_qty - cur_qty)
        if remaining == 0:
            break
        # 재주문 전에는 앞선 주문이 끝났는지부터 본다. 살아 있는 주문 위에 겹쳐 내면
        # 같은 물량을 두 번 산다 — 2026-08-24에 그날 체결된 2,097주 중 793주(38%)만
        # 폴링으로 보고 나머지를 다시 주문했다.
        if attempt > 1:
            if remaining > 0:
                # 매수는 대기 수량을 셀 방법이 없다. 잔고의 ord_psbl_qty는 매도 쪽만
                # 알려주고, 체결은 폴링보다 늦게 잡힌다 - 2026-08-25 일진홀딩스는
                # 32주로 보였지만 실제로는 142주가 체결됐다. 그 시점에 부족분을 다시
                # 냈다면 목표의 네 배를 샀을 것이다.
                print(f"[{MODE} 매수 중단] {code} {name} - 대기 수량을 셀 수 없어 "
                      f"추가 주문하지 않는다 (시도 {attempt}/{MAX_FILL_ATTEMPTS})")
                break
            pending = _pending_sell_qty(code)
            if pending is None:
                print(f"[{MODE} 매도 중단] {code} {name} - 대기 수량을 확인할 수 없어 "
                      f"추가 주문하지 않는다 (시도 {attempt}/{MAX_FILL_ATTEMPTS})")
                break
            if pending > 0:
                print(f"[{MODE} 매도 대기] {code} {name} - {pending:.0f}주가 아직 "
                      f"주문에 묶여 있다. 추가 주문 없음")
                break
        if remaining > 0:
            # 미수 방지: 이 매수 대금이 결제 예정 예수금을 음수로 만들면 넘긴다.
            # 시장가라 정확한 체결가는 알 수 없어 현재가 + 슬리피지·수수료 여유로 잡는다.
            est_px = snapshot["holdings"].get(code, {}).get("cur_px", 0.0)
            if est_px <= 0:
                last = db.fetchone(
                    "SELECT c FROM stock_daily WHERE code=%s ORDER BY d DESC LIMIT 1", (code,))
                est_px = float(last["c"]) if last else 0.0
            # 돈이 모자라면 건너뛰지 말고 살 수 있는 만큼만 산다. 슬롯 하나를
            # 통째로 비우는 것보다 덜 채우는 편이 목표 비중에 가깝다.
            unit = est_px * 1.005          # 시장가라 슬리피지·수수료 여유를 얹는다
            settled = _settled_cash()
            if unit <= 0 or settled < unit:
                print(f"[{MODE} 미수 방지] {code} {name} - 결제 예정 예수금 "
                      f"{settled:,.0f}으로는 1주도 못 산다 - 넘김")
                break
            if remaining * unit > settled:
                cut = int(settled // unit)
                print(f"[{MODE} 수량 축소] {code} {name} - 결제 예정 예수금 "
                      f"{settled:,.0f} → {remaining}주에서 {cut}주로 줄인다")
                remaining = cut
        result = (buy if remaining > 0 else sell)(code, abs(remaining))
        if result.get("rt_cd") != "0":
            print(f"[{MODE} {side} 실패] {code} {name} - {result.get('msg1')}")
            break
        after, new_qty = _wait_for_fill(code, cur_qty + remaining)
        if new_qty == cur_qty:
            print(f"[{MODE} {side} 지연] {code} {name} - {int(_WAIT_S)}초 안에 잔고에 "
                  f"안 잡혔다 (시도 {attempt}/{MAX_FILL_ATTEMPTS})")
        cur_qty = new_qty

    filled = cur_qty - cur
    if filled == 0:
        return

    # 매수 대금은 이 종목의 매입금액(수량 x 매입평균) 차분에서 구한다. 계좌 전체
    # 누적(thdt_buy_amt)의 증분은 종목 귀속이 보장되지 않는다 - 2026-08-26에
    # 앞선 종목(일진홀딩스)의 뒤늦은 체결이 증분에 섞여, 대한해운 매수가 그날
    # 상한가 2,795보다 높은 3,370으로 기록되고 일진홀딩스 4주는 통째로 누락됐다.
    # 매도는 매입평균이 바뀌지 않아 이 차분이 0이므로 계좌 전체 매도 증분을 쓴다.
    # 양쪽 다 pchs_avg_pric을 쓰던 시절에는 매도 기록의 단가가 산 가격이었다
    # (2026-08-25 확인, 8건 중 7건이 소수점까지 일치).
    if filled > 0:
        avg_after = float(_field(after, "pchs_avg_pric", code)) if after else 0.0
        delta = cur_qty * avg_after - cur * avg_before
    else:
        delta = _today_traded()[1] - traded_before[1]

    ref_px = float(snapshot["holdings"].get(code, {}).get("cur_px") or 0.0)
    px = delta / abs(filled) if delta > 0 else 0.0
    if px > 0 and ref_px > 0 and abs(px / ref_px - 1) > _PX_SANITY:
        print(f"[{MODE} 단가 이상] {code} {name} - 산출가 {px:,.0f}이 현재가 "
              f"{ref_px:,.0f}에서 {_PX_SANITY:.0%} 넘게 벗어나 버린다")
        px = 0.0
    if px <= 0:
        # 체결이 폴링 뒤에 잡혔거나 집계가 늦은 경우. 근사치임을 남긴다.
        px = ref_px or (float(_field(after, "pchs_avg_pric", code)) if after else 0.0)
        print(f"[{MODE} 단가 추정] {code} {name} - {px:,.0f}원으로 기록한다 "
              f"(실제 체결가 아님)")
    new_qty = cur_qty
    delta = int(target_qty - cur)

    if new_qty > 0:
        db.execute(
            """INSERT INTO positions (code, strategy, name, entry_date, entry_px, qty,
                                      stop_px, max_hold_days, mode, agents)
               VALUES (%s,%s,%s,%s,%s,%s,0,99999,%s,%s::jsonb)
               ON CONFLICT (code, strategy, mode) DO UPDATE
                 SET qty = EXCLUDED.qty, entry_px = EXCLUDED.entry_px,
                     agents = COALESCE(EXCLUDED.agents, positions.agents)""",
            (code, strategy, name, date.today(), px, new_qty, MODE,
             json.dumps(agents_summary, ensure_ascii=False) if agents_summary else None))
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
