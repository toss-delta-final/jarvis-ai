"""프로필 빌더 — 델타 생성 + sleep-time consolidation (SPEC-PROFILE-001 §6.2/6.4, 결정 4-A).

2단 비동기 쓰기:
  (1) 세션 종료 트리거 시 transient 세션 버퍼에서 후보 취향 델타를 LLM(Sonnet) 추출 → 게이트 승격
  (2) sleep-time consolidation: 승격 fact 를 §5.1 3섹션 요약 마크다운으로 재작성(recency-wins)
턴 중에는 write 하지 않고 세션 버퍼만 누적한다(transient 격리). "기억해" hot-path 만 즉시 기록.

LLM 은 주입형(테스트 fake) — 미구성/오류 시 best-effort degrade(프로필 미갱신, 다음 배치가 회수).
프로덕션은 PostgresStore 병합·미처리 스레드 스캔으로 이관(REQ-PROF-050/051).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum

from app.agents.profile.gate import should_promote
from app.agents.profile.resolver import resolve_triple
from app.agents.profile.store import get_profile_store
from app.agents.buyer.recommendation.state import extract_json
from app.core.config import Settings, get_settings
from app.core.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

# [#356] 구조화 제안 프롬프트. LLM 은 **식별 키를 만들지 않는다**(REQ-PGRAPH-011) — kind·label·
# anchorPhrase 같은 *제안*까지만 내고 node_id/edge_key/edge_id 확정은 resolver 가 한다.
# 근거는 #115 실측이다: LLM 이 라벨을 직접 확정하게 두면 **같은 발화에서 값이 흔들리고 오타가
# 난다.** 흔들리는 키에는 누적이 성립하지 않는다(SPEC-PROFILE-001 OPEN-P12 가 지적한 실패).
#
# 첫 줄의 "델타 추출기" 는 테스트 fake 들의 분기 문자열이다(tests/unit/test_profile.py) — 바꾸면
# 프로필 테스트가 전부 조용히 consolidation 응답을 받게 된다.
#
# fact 필드는 **그대로 둔다**. 요약 LLM 의 입력이자 fact 수준 dedup(문자열 완전 일치,
# REQ-PGRAPH-017)의 키라서, anchorPhrase 로 대체하면 같은 취향의 다른 발화가 전부 별개 fact 가
# 되고 요약 품질도 떨어진다.
_DELTA_SYSTEM = """당신은 커머스 어시스턴트의 취향 프로필 델타 추출기입니다.
세션 대화(사용자 발화 모음)에서 장기 보관할 만한 취향 신호만 뽑습니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{ "deltas": [ { "fact": "간결한 취향 서술(한국어)", "kind": "brand|category|attribute|priceBand|ratingBand|product|situation", "label": "대상 이름", "anchorPhrase": "발화에서 그대로 인용한 구절", "polarity": "positive|negative", "predicateHint": "prefers|likes|avoids|interestedIn", "salience": 0.0~1.0, "explicit": true|false, "repetitionEma": 0.0~1.0 } ] }
규칙:
- salience=현저성(중요/뚜렷할수록↑), explicit=사용자가 명시적으로 선호를 말함, repetitionEma=반복 정도.
- 일회성 잡담·잡음은 제외. 가격대·브랜드 선호/회피·카테고리·평점 성향 등 재사용 가능한 신호 위주.
- anchorPhrase 는 **발화에 실제로 있는 표현을 그대로** 인용하세요(요약·의역 금지).
- polarity 는 회피/비선호면 negative 입니다.
- priceBand·ratingBand 의 label 은 반드시 "최소-최대" 숫자 형식입니다(예: 가격 "30000-50000", 평점 "4-5").
  범위를 숫자로 특정할 수 없으면 그 항목의 kind 를 attribute 로 바꾸거나 제외하세요.
- product 의 label 은 숫자 상품 id 만 씁니다. 상품명은 kind 를 category 나 attribute 로 쓰세요.
- 없으면 {"deltas": []}."""

# 전환 이전 프롬프트 — `profile_graph_delta_enabled=False` 롤백 경로 (OPEN-G8).
# 프롬프트 변경은 동작 중인 LLM 계약을 바꾸는 일이고, 지시 한 줄이 기존 성공 케이스를 3/3 → 1/3 로
# 희석한 실측 전례가 있다(#198). 회귀가 보이면 배포 롤백 없이 이 경로로 되돌린다.
_DELTA_SYSTEM_LEGACY = """당신은 커머스 어시스턴트의 취향 프로필 델타 추출기입니다.
세션 대화(사용자 발화 모음)에서 장기 보관할 만한 취향 신호만 뽑습니다.
반드시 아래 JSON 만 출력하세요(설명·코드펜스 금지):
{ "deltas": [ { "fact": "간결한 취향 서술(한국어)", "salience": 0.0~1.0, "explicit": true|false, "repetitionEma": 0.0~1.0 } ] }
규칙:
- salience=현저성(중요/뚜렷할수록↑), explicit=사용자가 명시적으로 선호를 말함, repetitionEma=반복 정도.
- 일회성 잡담·잡음은 제외. 가격대·브랜드 선호/회피·카테고리·평점 성향 등 재사용 가능한 신호 위주.
- 없으면 {"deltas": []}."""

_CONSOLIDATE_SYSTEM = """당신은 커머스 취향 프로필 요약 작성기입니다.
아래 취향 fact 목록을 사람이 읽는 한국어 마크다운 요약으로 재작성하세요(중복 병합, 최신 우선).
3섹션 구성: (1) 구조화 블록(가격 성향·선호/회피 브랜드·평점·속성) (2) 취향 산문 (3) 최근 맥락.
confidence 수치·내부 메타는 노출하지 마세요. 마크다운만 출력(코드펜스 금지)."""


class ConsolidationResult(StrEnum):
    """프로필 요약 단계 결과 — 정상 no-op과 재시도가 필요한 실패를 구분한다."""

    UPDATED = "updated"
    NO_WORK = "no_work"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def record_remember(user_id: str, fact: str) -> None:
    """ "기억해" hot-path — 명시 명령은 게이트 없이 즉시 승격(REQ-PROF).

    발화 원문을 그대로 저장하되 config 길이 상한으로 절단한다(오탐·남용 시 무제한 누적 방어).
    """
    if not (user_id and fact):
        return
    settings = get_settings()
    cleaned = fact.strip()[: settings.profile_fact_char_cap]
    if cleaned:
        store = await get_profile_store()
        await store.add_fact(user_id, cleaned, cap=settings.profile_max_facts)


async def generate_session_delta(
    user_id: str,
    thread_key: str,
    *,
    profile_watermark: int,
    llm: LLMClient | None,
    settings: Settings,
) -> tuple[list[str], int] | None:
    """세션 버퍼(transient)에서 후보 델타를 LLM 추출 → 게이트 승격. (승격 fact 목록, 스냅샷 워터마크) 반환.

    반환: None = degrade(버퍼 없음/LLM 미구성, 버퍼 보존 신호), tuple = 처리됨(승격 fact 빈 목록 가능 + 워터마크).
    워터마크는 호출자가 clear_session_ctx_upto 로 "처리한 만큼만" 지우는 데 쓴다 — LLM 호출이 진행되는
    동안 버퍼에 새로 추가된 항목까지 통째로 삭제되는 레이스를 막기 위함(cap 트리밍으로 스냅샷 항목이
    먼저 밀려나도 seq 기준이라 안전).
    LLMError 는 전파 — 상위가 degrade 처리. 게스트는 호출 안 함(상위 책임).
    """
    store = await get_profile_store()
    buffer = await store.get_session_ctx_upto(thread_key, profile_watermark)
    if not buffer or llm is None:
        return None  # degrade(버퍼 없음/LLM 미구성) — 처리 안 함(상위가 버퍼 보존)
    structured = settings.profile_graph_delta_enabled
    # LLMError 는 전파 — 상위(events)가 degrade 로 처리해 버퍼를 보존(정상 반려와 구분).
    raw = await llm.complete(
        system=_DELTA_SYSTEM if structured else _DELTA_SYSTEM_LEGACY,
        user="\n".join(buffer),
        tier="smart",
        max_tokens=800,
    )
    data = extract_json(raw)
    promoted: list[str] = []
    for delta in data.get("deltas", []) if isinstance(data, dict) else []:
        if not isinstance(delta, dict):
            continue
        fact = str(delta.get("fact") or "").strip()
        if not fact:
            continue
        if should_promote(
            salience=_as_float(delta.get("salience")),
            explicit=bool(delta.get("explicit")),
            repetition_ema=_as_float(delta.get("repetitionEma")),
            threshold=settings.profile_gate_threshold,
        ):
            # 게이트 통과분만 resolve 한다 — 버려질 델타에 임베딩 왕복을 쓰지 않는다.
            triples = await _resolve_delta(delta, settings=settings) if structured else []
            await store.add_fact(
                user_id, fact, cap=settings.profile_max_facts, graph_triples=triples
            )
            promoted.append(fact)
    return promoted, profile_watermark


async def _resolve_delta(delta: dict, *, settings: Settings) -> list[dict]:
    """LLM 제안 하나를 확정 트리플로. 못 만들면 빈 목록(fact 는 그대로 저장된다).

    **여기서 실패해도 승격은 계속된다.** 그래프는 fact 위에 얹는 부가물이지 승격의 전제가
    아니다 — resolver 나 임베딩 백엔드 장애가 프로필 누적 자체를 멈추면 훨씬 큰 손실이다.
    트리플 없는 fact 는 문서에 실리지 않고 `unprojected_count` 로만 집계된다(REQ-PGRAPH-004).
    """
    if not delta.get("kind"):
        return []  # 구 스키마 응답(전환 중·롤백 후) — 정상 경로다
    try:
        resolved = await resolve_triple(
            kind=str(delta.get("kind") or ""),
            label=str(delta.get("label") or ""),
            anchor_phrase=str(delta.get("anchorPhrase") or ""),
            polarity=str(delta.get("polarity") or "positive"),
            predicate_hint=str(delta.get("predicateHint") or ""),
            settings=settings,
            now=_now_iso(),
        )
    except Exception:
        # 예외 문자열은 업스트림 상태를 유출할 수 있어 클래스명도 남기지 않는다(#141 규약).
        logger.warning("profile_delta_resolve_failed")
        return []
    if resolved is None:
        return []
    return [
        resolved.as_payload(
            salience=_as_float(delta.get("salience")),
            source="conversation",  # 이 경로의 유일한 출처 — purchase/user 생산자는 아직 없다
        )
    ]


async def consolidate(user_id: str, *, llm, settings) -> ConsolidationResult:
    """sleep-time — 승격 fact 를 §5.1 3섹션 요약 마크다운으로 재작성 후 결과 상태 반환.

    fact 없음은 정상 no-op, LLM 미구성·오류·빈 응답은 재시도 가능한 실패로 구분한다.
    """
    store = await get_profile_store()
    facts = await store.get_facts(user_id)
    if not facts:
        return ConsolidationResult.NO_WORK
    if llm is None:
        return ConsolidationResult.FAILED
    try:
        raw = await llm.complete(
            system=_CONSOLIDATE_SYSTEM,
            user="\n".join(facts),
            tier="smart",
            max_tokens=1000,
            json_output=False,  # 마크다운 요약 — OpenAI response_format=json 강제 금지(리뷰 #44)
        )
    except LLMError:
        return ConsolidationResult.FAILED
    markdown = (raw or "").strip()[: settings.profile_summary_max_chars]
    if not markdown:
        return ConsolidationResult.FAILED
    await store.set_summary(user_id, markdown, _now_iso())
    return ConsolidationResult.UPDATED


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
