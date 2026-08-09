import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

import db.connection as db
import config

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


def _pnl_chart_data(closed_trades, width=720, height=180, pad_x=8, pad_y=16):
    if not closed_trades:
        return None
    cum = 0.0
    series = []
    for t in closed_trades:
        cum += float(t["realized_pct"]) * 100
        series.append({"label": t["ts"].strftime("%m/%d"), "cum_pct": round(cum, 2)})

    values = [s["cum_pct"] for s in series]
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin, vmax = vmin - 1, vmax + 1

    n = len(series)
    for i, s in enumerate(series):
        s["x"] = round(pad_x + (i * (width - 2 * pad_x) / (n - 1) if n > 1 else (width - 2 * pad_x) / 2), 1)
        s["y"] = round(pad_y + (vmax - s["cum_pct"]) / (vmax - vmin) * (height - 2 * pad_y), 1)

    zero_y = None
    if vmin <= 0 <= vmax:
        zero_y = round(pad_y + (vmax - 0) / (vmax - vmin) * (height - 2 * pad_y), 1)

    polyline = " ".join(f"{s['x']},{s['y']}" for s in series)
    area = f"{pad_x},{height - pad_y} {polyline} {series[-1]['x']},{height - pad_y}"

    return {
        "points": series,
        "polyline": polyline,
        "area": area,
        "last_positive": values[-1] >= 0,
        "zero_y": zero_y,
        "width": width,
        "height": height,
    }


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

    closed_trades = db.fetchall("""
        SELECT ts, realized_pct FROM trades
        WHERE side = 'sell' AND realized_pct IS NOT NULL AND mode = %s
        ORDER BY ts ASC
    """, (mode,))
    pnl_chart = _pnl_chart_data(closed_trades)

    return templates.TemplateResponse(request, "index.html", {
        "positions": positions,
        "today_trades": today_trades,
        "total_unrealized": total_unrealized,
        "pnl_chart": pnl_chart,
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
