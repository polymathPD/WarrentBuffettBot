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


def _equity_chart_data(rows, width=720, height=260, pad_x=10, pad_top=18,
                       line_h=150, bar_h=48):
    """equity_daily 행(날짜 오름차순)으로 자산곡선 + 일별 수익률 막대를 계산.

    x축은 거래일 순서로 등간격이다(달력 간격이 아니라 기록이 있는 날만 잇는다).
    """
    if not rows:
        return None

    values = [float(r["total_equity"]) for r in rows]
    base = values[0]
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin, vmax = vmin * 0.999, vmax * 1.001

    n = len(values)
    span = width - 2 * pad_x
    line_bottom = pad_top + line_h
    bar_zero = line_bottom + 20 + bar_h / 2
    bar_w = max(2.0, min(14.0, span / n * 0.6))

    # 일별 수익률: 전일 총자산 대비. 첫날은 기준일이라 없음.
    rets = [None] + [values[i] / values[i - 1] - 1 for i in range(1, n)]
    max_abs = max((abs(r) for r in rets[1:]), default=0.0) or 0.01

    points = []
    for i, v in enumerate(values):
        x = pad_x + (i * span / (n - 1) if n > 1 else span / 2)
        y = pad_top + (vmax - v) / (vmax - vmin) * line_h
        r = rets[i]
        bar = None
        if r is not None:
            h = abs(r) / max_abs * (bar_h / 2)
            bar = {
                "x": round(x - bar_w / 2, 1),
                "y": round(bar_zero - h if r >= 0 else bar_zero, 1),
                "h": round(max(h, 0.6), 1),
                "up": r >= 0,
            }
        points.append({
            "label": rows[i]["d"].strftime("%m/%d"),
            "x": round(x, 1), "y": round(y, 1),
            "equity": round(v),
            "cum_pct": round((v / base - 1) * 100, 2),
            "day_pct": (round(r * 100, 2) if r is not None else None),
            "bar": bar,
        })

    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    area = f"{pad_x},{line_bottom} {polyline} {points[-1]['x']},{line_bottom}"

    return {
        "points": points,
        "polyline": polyline,
        "area": area,
        "last_positive": values[-1] >= base,
        "width": width,
        "height": height,
        "line_bottom": round(line_bottom, 1),
        "bar_zero": round(bar_zero, 1),
        "bar_w": round(bar_w, 1),
        "label_y": height - 6,
        "total_equity": round(values[-1]),
        "cum_pct": round((values[-1] / base - 1) * 100, 2),
        "cash": round(float(rows[-1]["cash"])),
        "positions_value": round(float(rows[-1]["positions_value"])),
    }


ALLOC_SLICES = 7   # 범주형 색상 슬롯 수. 초과분은 '기타'로 묶는다.


def _allocation_data(positions, cash, width=720, height=28, gap=2):
    """보유 종목별 평가금액 + 현금의 구성비(스택 바).

    종목이 색상 슬롯보다 많으면 작은 것부터 '기타'로 묶는다 — 색을 늘리지 않는다.
    """
    slices = []
    for p in positions:
        px = p["current_px"] or p["entry_px"]
        slices.append({
            "label": p["name"] or p["code"],
            "value": float(p["qty"]) * float(px),
            "kind": "series",
        })
    slices.sort(key=lambda s: -s["value"])

    if len(slices) > ALLOC_SLICES:
        tail = slices[ALLOC_SLICES:]
        slices = slices[:ALLOC_SLICES]
        slices.append({
            "label": f"기타 {len(tail)}종목",
            "value": sum(t["value"] for t in tail),
            "kind": "other",
        })

    # 현금이 음수면(자금 초과 집행) 구성비가 성립하지 않으므로 0으로 본다.
    slices.append({"label": "현금", "value": max(cash, 0.0), "kind": "cash"})

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


@app.get("/trades")
def trades_page(request: Request, mode: str = "paper"):
    mode = mode if mode in ("paper", "live") else "paper"
    trades = db.fetchall("""
        SELECT side, code, name, qty, price, amount,
               realized_pct, exit_reason, ts
        FROM trades WHERE mode = %s ORDER BY ts DESC LIMIT 50
    """, (mode,))
    return templates.TemplateResponse(request, "trades.html", {
        "trades": trades,
        "mode": mode,
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
):
    for key, value in [
        ("HEAT_AVOID", HEAT_AVOID),
        ("HEAT_SELL", HEAT_SELL),
        ("STOP_PCT", STOP_PCT),
        ("MAX_HOLD_DAYS", MAX_HOLD_DAYS),
        ("SLOTS", SLOTS),
    ]:
        db.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, str(value)),
        )
    config.invalidate_settings_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)
