import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse

import db.connection as db
import config

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


@app.get("/")
def index(request: Request):
    positions = db.fetchall("""
        SELECT p.code, p.name, p.entry_date, p.entry_px, p.qty, p.stop_px,
               sd.c AS current_px,
               ROUND((sd.c / p.entry_px - 1) * 100, 2) AS pct,
               ROUND((sd.c - p.entry_px) * p.qty, 0) AS unrealized
        FROM positions p
        LEFT JOIN stock_daily sd ON sd.code = p.code
          AND sd.d = (SELECT MAX(d) FROM stock_daily WHERE code = p.code)
        ORDER BY p.entry_date DESC
    """)
    today_trades = db.fetchall("""
        SELECT side, code, name, qty, price, realized_pct, exit_reason, ts
        FROM trades WHERE ts::date = CURRENT_DATE ORDER BY ts DESC
    """)
    total_unrealized = sum(float(p["unrealized"] or 0) for p in positions)
    return templates.TemplateResponse(request, "index.html", {
        "positions": positions,
        "today_trades": today_trades,
        "total_unrealized": total_unrealized,
    })


@app.get("/trades")
def trades_page(request: Request):
    trades = db.fetchall("""
        SELECT side, code, name, qty, price, amount,
               realized_pct, exit_reason, ts
        FROM trades ORDER BY ts DESC LIMIT 50
    """)
    return templates.TemplateResponse(request, "trades.html", {
        "trades": trades,
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
