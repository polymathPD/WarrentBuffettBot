"""전역 설정.

비밀값(키·계좌·DB 접속)은 환경변수에서, 운용 파라미터는 config.json에서 읽는다.
JSON은 이 모듈을 임포트할 때 한 번 읽히므로, 워커가 뜨는 시점에 로드된다.
파라미터를 바꾸려면 config.json을 고치고 스케줄러를 재시작한다 — 코드는 건드리지
않는다. defaults 쪽 6개는 settings 테이블로 런타임 변경도 가능하다(get_setting).
"""
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PARAMS_PATH = Path(__file__).parent / "config.json"


def load_params(path=None) -> dict:
    """운용 파라미터 JSON을 읽는다.

    없거나 깨졌으면 그대로 죽인다. 슬리피지나 자본금을 코드 기본값으로 조용히
    대신하면 그 값으로 주문이 나가고 백테스트가 돌아간다 — 설정을 못 읽은 것과
    설정이 0인 것은 구별돼야 한다.
    """
    with open(path or PARAMS_PATH, encoding="utf-8") as f:
        return json.load(f)


_params = load_params()

DB_URL = os.environ.get("DB_URL", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT = os.environ.get("KIS_ACCOUNT", "")
KIS_ACCOUNT_SUFFIX = os.environ.get("KIS_ACCOUNT_SUFFIX", "01")
KIS_MODE = os.environ.get("KIS_MODE", "paper")
DART_API_KEY = os.environ.get("DART_API_KEY", "")
KRX_OPEN_API_KEY = os.environ.get("KRX_OPEN_API_KEY", "")

# 비용 모델. 런타임 변경 대상이 아니다 — 백테스트와 실행이 같은 값을 봐야 한다.
SLIP_BPS = _params["costs"]["SLIP_BPS"]   # 슬리피지 편도
FEE_BPS = _params["costs"]["FEE_BPS"]     # 수수료
TAX_BPS = _params["costs"]["TAX_BPS"]     # 거래세 (매도 시)

# settings 테이블로 런타임 변경 가능한 값들의 기본값.
# CAPITAL은 전략 하나가 굴리는 자금이다. 1슬롯 = CAPITAL / SLOTS
_DEFAULTS = dict(_params["defaults"])

SLOTS = _DEFAULTS["SLOTS"]
HEAT_AVOID = _DEFAULTS["HEAT_AVOID"]
HEAT_SELL = _DEFAULTS["HEAT_SELL"]
MAX_HOLD_DAYS = _DEFAULTS["MAX_HOLD_DAYS"]
STOP_PCT = _DEFAULTS["STOP_PCT"]
CAPITAL = _DEFAULTS["CAPITAL"]

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
