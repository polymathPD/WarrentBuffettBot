import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

import db.connection as db
import config
from recorder.equity import cash_by_key

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


def _line_chart_data(rows, width=720, height=260, pad_x=10, pad_top=18,
                     line_h=150, bar_h=48):
    """선 + 구간 막대 차트의 좌표를 계산한다 (자산곡선·백테스트 누적곡선 공용).

    rows 각 원소: label(x축 표시), value(선), delta(막대, 없으면 None), tip(툴팁).
    x축은 기록이 있는 지점만 등간격으로 잇는다 (달력 간격이 아니다).
    """
    if not rows:
        return None

    values = [r["value"] for r in rows]
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin, vmax = vmin - abs(vmin) * 0.001 - 0.001, vmax + abs(vmax) * 0.001 + 0.001

    n = len(values)
    span = width - 2 * pad_x
    line_bottom = pad_top + line_h
    bar_zero = line_bottom + 20 + bar_h / 2
    bar_w = max(2.0, min(14.0, span / n * 0.6))

    deltas = [r.get("delta") for r in rows]
    has_bars = any(d is not None for d in deltas)
    max_abs = max((abs(d) for d in deltas if d is not None), default=0.0) or 1.0

    points = []
    for i, v in enumerate(values):
        x = pad_x + (i * span / (n - 1) if n > 1 else span / 2)
        y = pad_top + (vmax - v) / (vmax - vmin) * line_h
        d = deltas[i]
        bar = None
        if d is not None:
            h = abs(d) / max_abs * (bar_h / 2)
            bar = {
                "x": round(x - bar_w / 2, 1),
                "y": round(bar_zero - h if d >= 0 else bar_zero, 1),
                "h": round(max(h, 0.6), 1),
                "up": d >= 0,
            }
        points.append({
            "label": rows[i]["label"],
            "x": round(x, 1), "y": round(y, 1),
            "tip": rows[i]["tip"],
            "bar": bar,
        })

    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    area = f"{pad_x},{line_bottom} {polyline} {points[-1]['x']},{line_bottom}"

    return {
        "points": points,
        "polyline": polyline,
        "area": area,
        "last_positive": values[-1] >= values[0],
        "width": width,
        "height": height,
        "line_bottom": round(line_bottom, 1),
        "bar_zero": round(bar_zero, 1),
        "bar_w": round(bar_w, 1),
        "has_bars": has_bars,
        "label_y": height - 6,
    }


def _equity_chart_data(rows):
    """equity_daily 행(날짜 오름차순) → 자산곡선. 막대는 전일 대비 일별 수익률."""
    if not rows:
        return None

    values = [float(r["total_equity"]) for r in rows]
    base = values[0]
    day_rets = [None] + [values[i] / values[i - 1] - 1 for i in range(1, len(values))]

    points = []
    for i, v in enumerate(values):
        cum_pct = (v / base - 1) * 100
        tip = f"{rows[i]['d']:%m/%d}  {v:,.0f}원  누적 {cum_pct:+.2f}%"
        if day_rets[i] is not None:
            tip += f"  일간 {day_rets[i] * 100:+.2f}%"
        points.append({
            "label": f"{rows[i]['d']:%m/%d}",
            "value": v,
            "delta": day_rets[i],
            "tip": tip,
        })

    chart = _line_chart_data(points)
    chart.update({
        "total_equity": round(values[-1]),
        "cum_pct": round((values[-1] / base - 1) * 100, 2),
        "cash": round(float(rows[-1]["cash"])),
        "positions_value": round(float(rows[-1]["positions_value"])),
    })
    return chart


ALLOC_SLICES = 7   # 범주형 색상 슬롯 수. 초과분은 '기타'로 묶는다.


def _stack_bar(slices, width=720, height=28, gap=2):
    """구성비 스택 바. slices 각 원소: label, value, kind(series|other|cash)."""
    total = sum(s["value"] for s in slices)
    if total <= 0:
        return None

    n = len(slices)
    avail = width - gap * (n - 1)
    x = 0.0
    series_i = 0
    for s in slices:
        w = s["value"] / total * avail
        s["x"] = round(x, 1)
        s["w"] = round(max(w, 1.0), 1)
        s["pct"] = round(s["value"] / total * 100, 1)
        s["amount"] = round(s["value"])
        if s["kind"] == "series":
            series_i += 1
            s["cls"] = f"is-s{series_i}"
        else:
            s["cls"] = f"is-{s['kind']}"
        x += w + gap

    return {"slices": slices, "width": width, "height": height,
            "total": round(total)}


def _fold_tail(slices, limit=ALLOC_SLICES, unit="종목"):
    """색상 슬롯을 넘는 항목은 '기타'로 묶는다 — 색을 새로 만들지 않는다."""
    if len(slices) <= limit:
        return slices
    tail = slices[limit:]
    return slices[:limit] + [{
        "label": f"기타 {len(tail)}{unit}",
        "value": sum(t["value"] for t in tail),
        "kind": "other",
    }]


def _allocation_data(positions, cash):
    """보유 종목별 평가금액 + 현금의 구성비."""
    slices = [{
        "label": p["name"] or p["code"],
        "value": float(p["qty"]) * float(p["current_px"] or p["entry_px"]),
        "kind": "series",
    } for p in positions]
    slices.sort(key=lambda s: -s["value"])
    slices = _fold_tail(slices)

    # 현금이 음수면(자금 초과 집행) 구성비가 성립하지 않으므로 0으로 본다.
    slices.append({"label": "현금", "value": max(cash, 0.0), "kind": "cash"})
    return _stack_bar(slices)


@app.get("/")
def index(request: Request, mode: str = "paper"):
    mode = mode if mode in ("paper", "live") else "paper"
    positions = db.fetchall("""
        SELECT p.code, p.name, p.entry_date, p.entry_px, p.qty, p.stop_px,
               sd.c AS current_px,
               ROUND((sd.c / p.entry_px - 1) * 100, 2) AS pct,
               ROUND((sd.c - p.entry_px) * p.qty, 0) AS unrealized
        FROM positions p
        LEFT JOIN stock_daily sd ON sd.code = p.code
          AND sd.d = (SELECT MAX(d) FROM stock_daily WHERE code = p.code)
        WHERE p.mode = %s
        ORDER BY p.entry_date DESC
    """, (mode,))
    today_trades = db.fetchall("""
        SELECT side, code, name, qty, price, realized_pct, exit_reason, ts
        FROM trades WHERE ts::date = CURRENT_DATE AND mode = %s ORDER BY ts DESC
    """, (mode,))
    total_unrealized = sum(float(p["unrealized"] or 0) for p in positions)

    # 전략이 여럿이면 같은 날 행이 여러 개다 — 모드 전체 자산으로 합산한다.
    equity_rows = db.fetchall("""
        SELECT d, SUM(cash) AS cash,
               SUM(positions_value) AS positions_value,
               SUM(total_equity) AS total_equity
        FROM equity_daily WHERE mode = %s
        GROUP BY d ORDER BY d ASC
    """, (mode,))
    equity_chart = _equity_chart_data(equity_rows)

    cash = sum(v for (m, _), v in cash_by_key().items() if m == mode)
    allocation = _allocation_data(positions, cash)

    return templates.TemplateResponse(request, "index.html", {
        "positions": positions,
        "today_trades": today_trades,
        "total_unrealized": total_unrealized,
        "equity_chart": equity_chart,
        "allocation": allocation,
        "mode": mode,
    })


TRADE_DAYS = ("7", "30", "90", "all")
TRADE_LIMIT = 200


@app.get("/trades")
def trades_page(request: Request, mode: str = "paper", days: str = "30",
                side: str = "", reason: str = ""):
    mode = mode if mode in ("paper", "live") else "paper"
    days = days if days in TRADE_DAYS else "30"
    side = side if side in ("buy", "sell") else ""

    # 청산 사유는 전략마다 다르므로 목록을 DB에서 읽는다.
    reasons = [
        r["exit_reason"] for r in db.fetchall(
            "SELECT DISTINCT exit_reason FROM trades "
            "WHERE mode = %s AND exit_reason IS NOT NULL ORDER BY 1", (mode,)
        )
    ]
    reason = reason if reason in reasons else ""

    where = ["mode = %s"]
    params = [mode]
    if days != "all":
        where.append("ts >= NOW() - (%s || ' days')::interval")
        params.append(days)
    if side:
        where.append("side = %s")
        params.append(side)
    if reason:
        where.append("exit_reason = %s")
        params.append(reason)

    trades = db.fetchall(
        f"""SELECT side, code, name, qty, price, amount, strategy,
                   realized_pct, exit_reason, agents, ts
            FROM trades WHERE {' AND '.join(where)}
            ORDER BY ts DESC LIMIT {TRADE_LIMIT}""",
        tuple(params),
    )
    return templates.TemplateResponse(request, "trades.html", {
        "trades": trades,
        "mode": mode,
        "days": days,
        "side": side,
        "reason": reason,
        "reasons": reasons,
        "limit": TRADE_LIMIT,
    })


CURVE_MAX_POINTS = 200

SUMMARY_LABELS = {
    "n": "거래 수", "mean_pct": "평균 수익률(%)", "std_pct": "표준편차(%)",
    "win_rate": "승률(%)", "t_val": "t값", "mdd_pct": "MDD(복리, %)",
    "annualized_pct": "연환산(%)", "turnover_per_slot": "슬롯당 연 회전",
    "avg_held_days": "평균 보유(일)", "bootstrap_positive_pct": "부트스트랩 양수율(%)",
    "random_pct_rank": "무작위 대조군 상위(%)", "split_date": "기간분리 기준일",
    "t1_n": "전반 거래 수", "t2_n": "후반 거래 수",
    "t1_mean_pct": "전반 평균(%)", "t2_mean_pct": "후반 평균(%)",
}


def _backtest_curve(rows):
    """청산일별 수익률 합을 누적한 곡선.

    거래가 시간상 겹치므로 일별 자산을 복원할 수는 없다. 여기서는 '한 거래에
    같은 금액을 넣었다고 볼 때의 누적 수익률(단리 합)'을 그린다 — 복리로 접으면
    슬롯 제약이 없는 실행에서 값이 0으로 붕괴해 형태를 못 본다.
    """
    if not rows:
        return None, None

    step = max(1, len(rows) // CURVE_MAX_POINTS)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    points = []
    for i, r in enumerate(rows):
        day_pct = float(r["ret_sum"]) * 100
        cum += day_pct
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        if i % step and i != len(rows) - 1:
            continue
        points.append({
            "label": f"{r['exit_d']:%y/%m}",
            "value": round(cum, 2),
            "delta": day_pct,
            "tip": f"{r['exit_d']:%Y-%m-%d}  누적 {cum:+.1f}%p  "
                   f"당일 청산 {r['n']}건 {day_pct:+.2f}%p",
        })

    chart = _line_chart_data(points)
    chart["cum_pct"] = round(cum, 1)
    return chart, round(max_dd, 1)


@app.get("/backtest")
def backtest_page(request: Request, run: int = 0):
    runs = db.fetchall("""
        SELECT id, ts, strategy, start_d, end_d, params, summary
        FROM backtest_runs ORDER BY strategy
    """)

    selected = next((r for r in runs if r["id"] == run), None)
    if selected is None and runs:
        selected = runs[0]

    curve = mdd = reasons = quarters = None
    if selected:
        curve, mdd = _backtest_curve(db.fetchall("""
            SELECT exit_d, COUNT(*) AS n, SUM(ret_pct) AS ret_sum
            FROM backtest_trades WHERE run_id = %s
            GROUP BY exit_d ORDER BY exit_d
        """, (selected["id"],)))

        reason_rows = db.fetchall("""
            SELECT exit_reason, COUNT(*) AS n FROM backtest_trades
            WHERE run_id = %s GROUP BY exit_reason ORDER BY n DESC
        """, (selected["id"],))
        reasons = _stack_bar(_fold_tail([
            {"label": r["exit_reason"] or "-", "value": float(r["n"]), "kind": "series"}
            for r in reason_rows
        ], unit="가지"))

        quarter_rows = db.fetchall("""
            SELECT to_char(exit_d, 'YYYY"Q"Q') AS q, COUNT(*) AS n,
                   AVG(ret_pct) * 100 AS avg_pct
            FROM backtest_trades WHERE run_id = %s
            GROUP BY q ORDER BY q
        """, (selected["id"],))
        peak_abs = max((abs(float(q["avg_pct"])) for q in quarter_rows), default=1.0) or 1.0
        quarters = [{
            "q": q["q"], "n": q["n"], "avg_pct": round(float(q["avg_pct"]), 2),
            "bar_pct": round(abs(float(q["avg_pct"])) / peak_abs * 100, 1),
        } for q in quarter_rows]

    return templates.TemplateResponse(request, "backtest.html", {
        "runs": runs,
        "selected": selected,
        "curve": curve,
        "mdd": mdd,
        "reasons": reasons,
        "quarters": quarters,
        "labels": SUMMARY_LABELS,
    })


@app.get("/settings")
def settings_get(request: Request, saved: str = ""):
    settings = {k: config.get_setting(k) for k in config._DEFAULTS}
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "saved": saved == "1",
    })


@app.post("/settings")
def settings_post(
    HEAT_AVOID: float = Form(...),
    HEAT_SELL: float = Form(...),
    STOP_PCT: float = Form(...),
    MAX_HOLD_DAYS: int = Form(...),
    SLOTS: int = Form(...),
    CAPITAL: int = Form(...),
):
    for key, value in [
        ("HEAT_AVOID", HEAT_AVOID),
        ("HEAT_SELL", HEAT_SELL),
        ("STOP_PCT", STOP_PCT),
        ("MAX_HOLD_DAYS", MAX_HOLD_DAYS),
        ("SLOTS", SLOTS),
        ("CAPITAL", CAPITAL),
    ]:
        db.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, str(value)),
        )
    config.invalidate_settings_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)
