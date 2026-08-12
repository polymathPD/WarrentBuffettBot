import os
import time
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ.get("DB_URL", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT = os.environ.get("KIS_ACCOUNT", "")
KIS_ACCOUNT_SUFFIX = os.environ.get("KIS_ACCOUNT_SUFFIX", "01")
KIS_MODE = os.environ.get("KIS_MODE", "paper")
DART_API_KEY = os.environ.get("DART_API_KEY", "")
KRX_OPEN_API_KEY = os.environ.get("KRX_OPEN_API_KEY", "")

SLIP_BPS = 20       # 슬리피지 편도 0.20%
FEE_BPS = 1.5       # 수수료 0.015%
TAX_BPS = 20        # 거래세 0.20% (매도 시)

# 기본값 (DB settings 테이블로 런타임 변경 가능)
SLOTS = 12
HEAT_AVOID = 7.0
HEAT_SELL = 8.5
MAX_HOLD_DAYS = 20
STOP_PCT = 0.07

_DEFAULTS = {
    "SLOTS": SLOTS,
    "HEAT_AVOID": HEAT_AVOID,
    "HEAT_SELL": HEAT_SELL,
    "MAX_HOLD_DAYS": MAX_HOLD_DAYS,
    "STOP_PCT": STOP_PCT,
}

_settings_cache: dict = {}
_cache_ts: float = 0.0
_CACHE_TTL = 60


def _refresh_settings():
    global _settings_cache, _cache_ts
    if not DB_URL:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        conn.close()
        _settings_cache = {r[0]: r[1] for r in rows}
        _cache_ts = time.time()
    except Exception:
        pass


def get_setting(key: str):
    global _cache_ts
    if time.time() - _cache_ts > _CACHE_TTL:
        _refresh_settings()
    default = _DEFAULTS[key]
    val = _settings_cache.get(key)
    if val is None:
        return default
    try:
        return type(default)(val)
    except (ValueError, TypeError):
        return default


def invalidate_settings_cache():
    global _cache_ts
    _cache_ts = 0.0
