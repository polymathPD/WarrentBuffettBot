"""
Claude API 호출 기반 에이전트 베이스
- 입력 해시로 캐싱 (agent_decisions 테이블)
- 출력 형식: "결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hashlib, json, re
import anthropic
import db.connection as db
import config

MODEL = "claude-sonnet-4-6"
_client = None

# 호출 실패는 '관망'과 섞지 않는다. 조용히 관망으로 떨어뜨리면 게이트의 거부권과
# 구분이 안 돼, 크레딧이 떨어진 날에도 '시장이 안 좋아 안 샀다'로 보인다.
ERROR_DECISION = "오류"
ERROR_SEP = " - "

# 실패 사유 라벨. 대시보드가 이 라벨로 배너를 묶으므로 문구를 바꾸면 묶음도 바뀐다.
ERR_NO_KEY = "API 키 미설정"
ERR_CREDIT = "크레딧 잔액 부족"
ERR_AUTH = "API 키 인증 실패"
ERR_RATE = "요청 한도 초과"
ERR_NETWORK = "API 연결 실패"
ERR_OTHER = "API 오류"


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.CLAUDE_API_KEY)
    return _client


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _parse(text: str) -> tuple[str, float, str]:
    """'결정: X / 확신: Y / 이유: Z' 형식 파싱"""
    decision = re.search(r"결정\s*:\s*(\S+)", text)
    score = re.search(r"확신\s*:\s*([0-9.]+)", text)
    reason = re.search(r"이유\s*:\s*(.+)", text)

    d = decision.group(1).strip() if decision else "관망"
    s = float(score.group(1)) if score else 5.0
    r = reason.group(1).strip() if reason else text[:200]
    return d, s, r


def _classify(exc: Exception) -> str:
    """예외를 사람이 읽을 실패 사유로 바꾼다.

    크레딧 소진은 401 authentication_error가 아니라 400 invalid_request_error로
    온다. 키가 잘못된 경우와 대응이 갈리므로(충전 vs 키 교체) 먼저 구분한다.
    """
    text = str(exc)
    if "credit balance" in text or getattr(exc, "type", None) == "billing_error":
        return ERR_CREDIT
    if isinstance(exc, anthropic.AuthenticationError):
        return ERR_AUTH
    if isinstance(exc, anthropic.RateLimitError):
        return ERR_RATE
    if isinstance(exc, anthropic.APIConnectionError):
        return ERR_NETWORK
    return ERR_OTHER


def _fail(agent_name: str, code: str, label: str, detail: str) -> dict:
    """실패를 agent_decisions에 남기고 '오류' 결정을 반환한다.

    input_hash는 NULL로 둔다 — 캐시 조회가 input_hash=%s이므로 실패한 판단이
    캐시에 얹혀 이후 호출까지 오염시키는 일이 없다.
    """
    rationale = f"{label}{ERROR_SEP}{detail}"
    db.execute(
        """INSERT INTO agent_decisions
           (code, agent, score, decision, rationale, model, input_hash)
           VALUES (%s, %s, %s, %s, %s, %s, NULL)""",
        (code, agent_name, 0.0, ERROR_DECISION, rationale, MODEL),
    )
    print(f"[에이전트 실패] {agent_name} {code} - {rationale}")
    return {"decision": ERROR_DECISION, "score": 0.0,
            "rationale": rationale, "error": label}


def call(agent_name: str, code: str, prompt: str,
         cache_scope: str = None) -> dict:
    """
    Claude API 호출. 동일 입력은 캐시에서 반환.
    반환: {"decision": str, "score": float, "rationale": str}

    cache_scope: 캐시 키에서 code 대신 쓸 값. 프롬프트에 종목이 들어가지 않는
    에이전트(시장 전체 판단 등)는 고정값을 넘겨, 같은 질문을 후보 종목 수만큼
    반복 결제하지 않게 한다. agent_decisions.code에는 그대로 code가 남는다.
    """
    h = _hash(agent_name + (code if cache_scope is None else cache_scope) + prompt)

    cached = db.fetchone(
        "SELECT decision, score, rationale FROM agent_decisions "
        "WHERE input_hash=%s ORDER BY ts DESC LIMIT 1",
        (h,),
    )
    if cached:
        return dict(cached)

    if not config.CLAUDE_API_KEY:
        return _fail(agent_name, code, ERR_NO_KEY, "CLAUDE_API_KEY 환경변수가 비어 있음")

    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        return _fail(agent_name, code, _classify(e), str(e))

    decision, score, rationale = _parse(text)

    db.execute(
        """INSERT INTO agent_decisions (code, agent, score, decision, rationale, model, input_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (code, agent_name, score, decision, rationale, MODEL, h),
    )

    return {"decision": decision, "score": score, "rationale": rationale}
