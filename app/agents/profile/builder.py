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
from app.agents.profile.graph_merge import build_graph_document, empty_document
from app.agents.profile.graph_models import GraphDocument
from app.agents.profile.personalization_gate import personalization_enabled
from app.agents.profile.resolver import resolve_triple
from app.agents.profile.store import FactRecord, get_profile_store
from app.agents.buyer.recommendation.state import extract_json
from app.core.config import Settings, get_settings
from app.core.llm import LLMClient, LLMError
from app.core.pii import contains_hard_pii

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
  한쪽 경계만 말했으면 **모르는 쪽을 비워 두세요** — 없는 경계를 지어내지 마세요.
  예: "5만원 이하"→"-50000", "10만원 이상"→"100000-", "평점 4점 이상"→"4-".
  양쪽 다 숫자로 특정할 수 없으면 그 항목의 kind 를 attribute 로 바꾸거나 제외하세요.
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

    [이슈 #321] 하드 PII(SPEC-PROFILE-GRAPH-149 REQ-PGRAPH-071)는 절단 **이전** 원문에서
    검사한다 — 절단이 먼저면 `010-1234-`처럼 번호가 잘려 정규식이 못 잡는다. 히트하면
    저장하지 않고 조용히 반환한다(hot-path 500 방지 — pii.contains_hard_pii 는 예외를
    던지지 않는다).
    """
    if not (user_id and fact):
        return
    stripped = fact.strip()
    if not stripped or contains_hard_pii(stripped):
        return
    settings = get_settings()
    cleaned = stripped[: settings.profile_fact_char_cap]
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

    **[#359] 개인화 중지면 델타를 뽑지 않되 "처리됨"으로 보고한다**(REQ-PGRAPH-052/053).
    반환값 둘의 의미가 정반대라 여기가 갈림길이다 — `None` 을 돌려주면 호출자가 RETRYABLE 로
    빠지고 버퍼가 남아 **같은 자리에서 sweep 이 영구 재시도**한다(세션 라이프사이클 정지).
    반대로 **판정 불가**(플래그 조회 실패)에 튜플을 돌려주면 호출자가 `clear_session_ctx_upto`
    까지 진행해 **DB 블립 한 번에 개인화가 켜져 있는 사용자의 누적 버퍼가 영구 삭제**된다.
    그래서 중지 확인 = 튜플, 판정 불가 = `None` 이다(`personalization_gate` 의 `on_error=None`).
    """
    # 플래그를 **LLM 왕복 앞**에서 본다 — 중지 중에 비용을 쓰면서 결과를 버릴 이유가 없다.
    enabled = await personalization_enabled(user_id, on_error=None)
    if enabled is None:
        return None  # 판정 불가 — degrade(버퍼 보존), 다음 sweep 재시도
    store = await get_profile_store()
    buffer = await store.get_session_ctx_upto(thread_key, profile_watermark)
    if not buffer or llm is None:
        return None  # degrade(버퍼 없음/LLM 미구성) — 처리 안 함(상위가 버퍼 보존)
    if not enabled:
        # 처리됨(승격 0건) — 호출자가 버퍼를 정리하고 claim 을 완료한다(REQ-PGRAPH-053).
        return [], profile_watermark
    structured = settings.profile_graph_delta_enabled
    # LLMError 는 전파 — 상위(events)가 degrade 로 처리해 버퍼를 보존(정상 반려와 구분).
    raw = await llm.complete(
        system=_DELTA_SYSTEM if structured else _DELTA_SYSTEM_LEGACY,
        user="\n".join(buffer),
        tier="smart",
        max_tokens=settings.profile_delta_max_tokens,
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
    """sleep-time — 그래프를 산출하고, 살아 있는 취향만으로 §5.1 요약을 재작성한다.

    **[HARD] 요약 입력은 그래프에서 나온다**(REQ-PGRAPH-023). fact 목록을 그대로 읽으면 다음
    배치가 삭제된 취향을 요약에 다시 써넣어 삭제 기능이 겉모습만 남는다.

    잠금 규약: 그래프 RMW 만 `graph_lock` 안에서 하고 **LLM 왕복은 락 밖**이다. 요약 쓰기는
    `set_summary` 가 자기 잠금을 잡으므로(#323) 여기서 락을 쥐고 있으면 advisory 풀에서
    커넥션을 동시에 둘 점유해, 동시 세션 종료 몇 건으로 구매자 턴 경로까지 말라 죽는다.

    반환 계약은 유지한다(finalizer 의 RETRYABLE 분기가 여기 의존한다): 할 일 없음은 NO_WORK,
    LLM 미구성·오류·빈 응답은 FAILED.

    **[#359] 개인화 중지면 아무것도 만들지 않는다**(REQ-PGRAPH-052 — 그래프·요약·임베딩).
    `NO_WORK` 는 finalizer 가 이미 정상 종료로 처리하는 값이라 라이프사이클이 멈추지 않는다
    (`FAILED` 만 RETRYABLE 로 간다). **판정 불가는 중지로 접지 않는다** — `NO_WORK` 로 돌리면
    pg 블립이 지속되는 동안 켜진 사용자의 프로필이 영영 갱신되지 않는데 로그 말고는 드러날
    신호가 없다. 종전대로 진행하고 실패는 자기 경로에서 나게 둔다.
    부수로 중지 기간에는 감쇠도 진행되지 않는다(REQ-PGRAPH-055) — 배치가 안 돌기 때문인데,
    그것만으로는 부족하고 재개 시 누적 오프셋 차감이 필요하다(C8).
    """
    # [#359] 플래그와 중지 구간을 **한 번에** 읽는다 — 같은 단일 행이라 왕복을 나눌 이유가 없다.
    # 구간은 감쇠에서 차감된다(REQ-PGRAPH-055): 중지 중 배치를 멈추는 것만으로는 부족하다.
    # `_confidence` 는 `decay_evaluated_at` 을 안 보고 관측 시각부터 `now` 까지 매 배치 새로
    # 계산하므로, 6개월 중지 후 재개하면 첫 배치에서 6개월치가 그대로 걸린다.
    from app.agents.profile import graph_journal

    paused_spans: list[dict] = []
    try:
        state = await graph_journal.get_personalization_state(user_id=int(user_id))
    except (TypeError, ValueError):
        state = None  # 숫자가 아닌 신원 — 플래그 대상이 아니다(게이트와 같은 규약)
    except Exception:
        # 조회 실패는 **중지로 접지 않는다** — NO_WORK 로 돌리면 pg 블립이 지속되는 동안 켜진
        # 사용자의 프로필이 영영 갱신되지 않는데 로그 말고는 드러날 신호가 없다.
        logger.warning("personalization_state_unavailable", exc_info=True)
        state = None
    if state is not None:
        if not state.enabled:
            return ConsolidationResult.NO_WORK
        paused_spans = state.disabled_spans

    store = await get_profile_store()
    now = _now_iso()

    async with store.graph_lock(user_id):
        facts = await store.get_fact_records(user_id)
        if not facts:
            return ConsolidationResult.NO_WORK
        existing = await store.get_graph(user_id) or await _bootstrap_document(user_id, now)
        document = build_graph_document(
            facts,
            existing=existing,
            settings=settings,
            now=now,
            decay_pause_spans=paused_spans,
        )
        await store.set_graph(user_id, document)
        summary_input = _summary_input(document, facts)
        # **LLM 왕복 전에** 요약 seq 를 읽어 둔다 (#323 잔여, #358). 아래 `set_summary` 는 락을
        # 놓고 수 초 뒤에 실행되므로, 그 창에서 사용자가 요약을 고치면 이 낡은 스냅샷이 그
        # 편집을 덮는다 — 두 쓰기가 시간상 겹치지 않아 잠금으로는 안 닫히는 갭이다.
        observed = await store.get_summary(user_id)
        expected_seq = observed.seq if observed else 0

    if not summary_input:
        # **기존 요약을 지우지 않는다.** 빈 문자열로 덮으면 마이페이지·rerank 는 취향을 잃는데
        # 홈 랭킹은 캐리오버된 옛 벡터로 계속 개인화한다(store.set_summary 주석) — 억제의
        # 정반대 상태다. 쓸 내용이 없으면 아무것도 안 하는 것이 맞다.
        return ConsolidationResult.NO_WORK
    if llm is None:
        return ConsolidationResult.FAILED
    try:
        raw = await llm.complete(
            system=_CONSOLIDATE_SYSTEM,
            user="\n".join(summary_input),
            tier="smart",
            max_tokens=settings.profile_summary_max_tokens,
            json_output=False,  # 마크다운 요약 — OpenAI response_format=json 강제 금지(리뷰 #44)
        )
    except LLMError:
        return ConsolidationResult.FAILED
    markdown = (raw or "").strip()[: settings.profile_summary_max_chars]
    if not markdown:
        return ConsolidationResult.FAILED
    if not await store.set_summary(user_id, markdown, now, expected_seq=expected_seq):
        # 그 사이 사용자가 요약을 고쳤다 — 낡은 스냅샷으로 덮지 않고 물러난다. 다음 배치가
        # 새 상태를 읽고 다시 만든다. 그래프는 이미 갱신됐으므로 실패가 아니라 no-op 이다.
        return ConsolidationResult.NO_WORK
    return ConsolidationResult.UPDATED


async def _bootstrap_document(user_id: str, now: str) -> GraphDocument:
    """문서가 없을 때 시작점 — **`revision` 을 되돌리지 않는다** (REQ-PGRAPH-042).

    `get_graph` 는 스키마가 깨진 문서를 조용히 `None` 으로 돌려준다(배치를 죽이지 않으려고).
    그때 `empty_document()` 를 그대로 쓰면 revision 이 0 이 되고, 이미 발급된 `If-Match: "g43"`
    이 다른 상태를 가리키게 된다 — 같은 토큰이 서로 다른 문서를 뜻하는 순간 낙관적 동시성이
    무너진다. 감사 하한(#358)은 초기화·손상 어느 쪽으로도 사라지지 않으므로 거기서 이어받는다.

    감사 조회가 실패해도 배치를 죽이지 않는다 — 개인화가 멈추는 것보다 낫다. 그 경우에만
    revision 0 으로 시작하며, 사용자 편집 경로는 CAS 로 자기 방어한다.
    """
    from app.agents.profile import graph_journal

    document = empty_document(now)
    try:
        floor = await graph_journal.next_revision(user_id=int(user_id), existing=None)
    except (ValueError, TypeError):
        return document  # 게스트 등 숫자가 아닌 신원 — 감사 대상이 아니다
    except Exception:
        logger.warning("profile_graph_revision_floor_unavailable")
        return document
    return document.model_copy(update={"revision": max(0, floor - 1)})


def _summary_input(document: GraphDocument, facts: list[FactRecord]) -> list[str]:
    """요약 LLM 입력 = 살아 있는 취향의 근거 fact + 트리플이 없는 fact.

    **사용자가 지운 취향(tombstone)과 `superseded` edge 는 즉시 제외**한다(REQ-PGRAPH-022).
    삭제된 edge 는 문서에서 물리 삭제되므로(#499) `edges` 조회로는 잡히지 않는다 — 그러면
    "문서에 없는 edge_key 는 통과"라는 아래 규칙에 걸려 **지운 취향이 요약으로 되돌아온다.**
    그래서 `document.tombstones` 를 별도로 본다. 그 edge 가 근거로 삼은
    fact 원문도 함께 뺀다 — 원문이 잔여 fact 로 새어 들어가면 edge 만 숨기고 내용은 그대로
    요약되는 셈이라 억제가 무의미해진다. 한 fact 가 여러 취향을 담고 그중 하나만 지워졌어도
    **통째로** 뺀다: 그 원문에는 지운 취향이 그대로 적혀 있어 살리면 되살아난다(삭제가 이긴다).

    트리플이 없는 fact("기억해" hot-path·resolver 드롭분·전환 이전 fact)는 **남긴다**. 그래프에는
    안 실리지만(unprojected_count) 개인화에서까지 통째로 사라지면, 사용자가 명시적으로 기억하라고
    말한 내용이 소리 없이 버려진다.

    **판정은 fact 자신의 트리플로 한다 — edge 의 `evidence_refs` 를 보지 않는다**(PR #410 리뷰).
    그 목록은 `graph_evidence_refs_max` 로 잘린 *저장용* 참조라, 재사용하면 같은 취향을 상한보다
    많이 언급한 순간 **지우지도 않은 활성 취향의 오래된 근거가 요약에서 조용히 빠진다**. 저장
    폭주 방지용 상한이 개인화 내용을 결정해서는 안 된다 — 상한 값을 바꾸면 요약이 함께 바뀐다.
    """
    status_by_key = {edge.edge_key: edge.status for edge in document.edges}
    tombstoned = {tombstone.edge_id for tombstone in document.tombstones}

    selected: list[str] = []
    for record in facts:
        # 문서에 없는 edge_key(절단 등)는 삭제 신호가 아니므로 통과시킨다 — 저장 한계가
        # 개인화 내용을 줄이지 않게 하는 것이 위 규칙과 같은 취지다. **단 tombstone 은 다르다** —
        # "문서에 없음"이 저장 한계가 아니라 사용자 삭제라는 사실이라, 여기서 갈라야 한다.
        if any(triple.get("edge_id") in tombstoned for triple in record.graph_triples):
            continue
        statuses = (
            status_by_key.get(triple.get("edge_key"), "active") for triple in record.graph_triples
        )
        if any(status != "active" for status in statuses):
            continue
        selected.append(record.fact)
    return selected


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
