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
        return {"decision": "관망", "score": 5.0, "rationale": "API 키 미설정 — 기본값 반환"}

    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        return {"decision": "관망", "score": 5.0, "rationale": f"API 오류: {e}"}

    decision, score, rationale = _parse(text)

    db.execute(
        """INSERT INTO agent_decisions (code, agent, score, decision, rationale, model, input_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (code, agent_name, score, decision, rationale, MODEL, h),
    )

    return {"decision": decision, "score": score, "rationale": rationale}
