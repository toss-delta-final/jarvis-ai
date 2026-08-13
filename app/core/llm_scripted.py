"""결정론 스크립트 LLM — E2E 스모크(`ScriptedLLM`, 이슈 #35) + 부하 테스트(`LoadTestLLM`, #438).

`ScriptedLLM`/`DEFAULT_*`/마커 상수는 `tests/integration/_stubs.py` 에서 **이동**했다(사본 아님) —
`app/` 런타임(`get_llm`, #438 G5)이 이 심볼을 써야 하는데 `Dockerfile` 은 `tests/` 를 이미지에
넣지 않아 `app/` 이 `tests.*` 를 import 하면 컨테이너에서 `ModuleNotFoundError` 가 난다. 정의를
여기 하나만 두고 `tests/integration/_stubs.py` 는 재수출만 한다(기존 10개 import 지점 무변경).

`LoadTestLLM` 은 `LLM_PROVIDER=scripted` 로 뜬 서버가 실제로 쓰는 구현체다. `ScriptedLLM` 의 고정
rerank(productId 101/102)를 그대로 쓰면 실 카탈로그에서 후보 id 가 하나도 안 맞아 항상
`LLMError`(degrade)로 떨어진다 — 부하 테스트가 "정상 경로 p95" 대신 "degrade 경로 p95" 를
재게 된다. `LoadTestLLM` 은 응답을 **프롬프트에서 유도**해 각 파서가 실제로 받아들이는 최소 유효
JSON 을 낸다(예: rerank 는 CANDIDATES 의 productId 를 그대로 되돌려준다).

**분류 실패는 조용한 폴백이 아니라 `LLMError` 로 소리 나게 던진다** — `ScriptedLLM._classify` 는
미상 프롬프트를 tier 로 decompose/rerank 로 떨어뜨리지만, `LoadTestLLM` 이 그러면 새 LLM 호출
지점이 생겼을 때 "엉뚱한 스키마를 조용히 돌려주고 그 레인만 degrade" 가 되어 부하 측정 자체가
거짓말이 된다(#438 D3).
"""

from __future__ import annotations

import asyncio
import json

from app.core.config import ScriptedLLMMode
from app.core.llm import LLMError, LOADTEST_MODEL_IDS
from app.core.tracing import current_request_trace

# ── E2E 스모크 스크립트 LLM (이슈 #35) — enrichment/프로필 시스템 프롬프트의 식별 문구
# (app/pipelines/enrichment.py·app/agents/profile/builder.py). `LoadTestLLM` 도 이 마커를
# 그대로 재사용한다(D3 — enrich/delta/consolidate 는 `ScriptedLLM` 의 기존 DEFAULT_* 그대로).
_ENRICH_MARK = "상품 태깅기"
_DELTA_MARK = "델타 추출기"
_CONSOLIDATE_MARK = "요약 작성기"

DEFAULT_DECOMPOSE = {
    "intent": "recommend",
    "reply": "",
    "case": 2,
    "semanticQuery": "여행용 파우치",
    # category 는 이제 filters 가 아니라 categoryQueries 로(이슈 #59, 새 스키마 정합)
    "categoryQueries": [{"category": "여행용품", "query": "여행 파우치"}],
    "filters": {"priceMax": 30000, "keyword": "여행 파우치"},
}

DEFAULT_RERANK = {
    "ranked": [
        {"productId": 102, "rationale": "기내 반입 규격에 맞아요"},
        {"productId": 101, "rationale": "방수라 세면도구에 좋아요"},
    ],
    "overallComment": "여행에 맞는 파우치를 골랐어요",
}

DEFAULT_ENRICH = {"tags": ["여행", "방수", "기내반입"], "attributes": {"소재": "방수 원단"}}

DEFAULT_DELTA = {
    "deltas": [
        {
            "fact": "3만원 이하 여행용품을 선호한다",
            "salience": 0.9,
            "explicit": True,
            "repetitionEma": 0.8,
        }
    ]
}

DEFAULT_PROFILE_MD = "## 취향 요약\n- 3만원 이하 여행용품 선호\n"


class ScriptedLLM:
    """호출 5종(decompose·rerank·enrich·profile delta·consolidate)을 프롬프트로 분기하는 fake.

    tests/_fakes.py 의 FakeLLM 은 모델 id 만 보고 2종을 분기하므로 배치·프로필까지 함께 도는
    E2E 에는 부족하다. 여기서는 **system 프롬프트 시그니처**로 용도를 판정한다.
    실패 주입(*_error)으로 degrade 경로(LLM_UNAVAILABLE·LLM_TIMEOUT·rerank 폴백)를 재현한다.
    """

    def __init__(
        self,
        *,
        decompose: dict | None = None,
        rerank: dict | None = None,
        enrich: dict | None = None,
        delta: dict | None = None,
        profile_markdown: str = DEFAULT_PROFILE_MD,
        decompose_error: bool = False,
        rerank_error: bool = False,
        timeout: bool = False,
    ) -> None:
        self._decompose = DEFAULT_DECOMPOSE if decompose is None else decompose
        self._rerank = DEFAULT_RERANK if rerank is None else rerank
        self._enrich = DEFAULT_ENRICH if enrich is None else enrich
        self._delta = DEFAULT_DELTA if delta is None else delta
        self._profile_markdown = profile_markdown
        self._decompose_error = decompose_error
        self._rerank_error = rerank_error
        self._timeout = timeout
        self.calls: list[tuple[str, str]] = []  # (kind, tier)
        # (kind, max_tokens, reasoning_effort) — #325 enrichment 토큰 예산 배선 회귀 검증용.
        self.complete_kwargs: list[tuple[str, int, str | None]] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        kind = self._classify(system, tier)
        self.calls.append((kind, tier))
        self.complete_kwargs.append((kind, max_tokens, reasoning_effort))
        if kind == "enrich":
            return json.dumps(self._enrich, ensure_ascii=False)
        if kind == "delta":
            return json.dumps(self._delta, ensure_ascii=False)
        if kind == "consolidate":
            return self._profile_markdown
        if kind == "decompose":
            if self._decompose_error:
                raise LLMError("timeout" if self._timeout else "decompose boom")
            return json.dumps(self._decompose, ensure_ascii=False)
        if self._rerank_error:
            raise LLMError("rerank boom")
        payload = (
            _scored_rerank_payload(self._rerank) if _RERANK_SCORING_MARK in system else self._rerank
        )
        return json.dumps(payload, ensure_ascii=False)

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "네, 도와드릴게요."

    @staticmethod
    def _classify(system: str, tier: str) -> str:
        """system 프롬프트 시그니처 → 호출 용도. 미상은 tier 로 decompose/rerank 판정."""
        if _ENRICH_MARK in system:
            return "enrich"
        if _DELTA_MARK in system:
            return "delta"
        if _CONSOLIDATE_MARK in system:
            return "consolidate"
        return "decompose" if tier == "fast" else "rerank"

    def calls_of(self, kind: str) -> int:
        """용도별 호출 횟수 — LLM 호출 예산(§llm_call_limit) 확인용."""
        return sum(1 for k, _ in self.calls if k == kind)


# ── 부하 테스트 스크립트 LLM (#438) ──

# 각 호출 지점 `_SYSTEM` 프롬프트의 고유 문구 — §5 테스트가 실제 `_SYSTEM` 을 import 해 이 마커가
# 여전히 들어 있는지 고정한다. decompose 프롬프트 파일은 이 이슈 범위 밖(#443/#465 소유)이라
# 문구를 고치지 않고 **현재 문구를 그대로 읽어서** 마커로 쓴다.
_DECOMPOSE_MARK = "질의 분해기"  # app/agents/buyer/recommendation/decompose.py::_SYSTEM
_RERANK_MARK = "재랭킹기"  # app/agents/buyer/recommendation/rerank.py::_SYSTEM
_CATEGORY_SELECT_MARK = (
    "카테고리 매퍼"  # app/agents/buyer/recommendation/category_select.py::_SYSTEM
)
_CATEGORY_SCOPE_MARK = '"scopeFree"'  # app/agents/buyer/recommendation/category_scope.py::_SYSTEM
_NEED_PRIORITY_MARK = '"priorities"'  # app/agents/buyer/recommendation/need_priority.py::_SYSTEM
_NEEDS_EXPANSION_MARK = '"items"'  # app/agents/buyer/recommendation/needs_expansion.py::_SYSTEM
_RERANK_SCORING_MARK = (
    "커머스 추천 평가기"  # app/agents/buyer/recommendation/rerank.py::_SYSTEM_STRUCTURED_SCORING
)

_USER_MESSAGE_MARKER = "USER_MESSAGE: "
_CANDIDATES_MARKER = "CANDIDATES: "
_NEEDS_MARKER = "NEEDS: "
# category_select.py 의 CANDIDATES 형식은 rerank 와 다르다 — `CANDIDATES:\n` 뒤에 콜론+공백이
# 아니라 개행이 오고, JSON 이 아니라 `- 후보` 줄 목록이 끝까지 이어진다(#438 R2 F2).
_CATEGORY_CANDIDATES_MARKER = "CANDIDATES:\n"


def _scored_rerank_payload(payload: dict) -> dict:
    """Adapt legacy scripted ranked rows to the production scored-rerank contract."""

    if isinstance(payload.get("evaluations"), list):
        return payload
    ranked = payload.get("ranked")
    if not isinstance(ranked, list):
        return payload
    evaluations = []
    for index, raw_item in enumerate(ranked):
        if not isinstance(raw_item, dict):
            evaluations.append(raw_item)
            continue
        target_score = max(0, 22 - index * 2)
        intent_fit = min(4, target_score // 4)
        need_fit = min(3, (target_score - intent_fit * 4) // 2)
        evaluations.append(
            {
                **raw_item,
                "intentFit": intent_fit,
                "needFit": need_fit,
                "profileFit": 0,
                "reasonCode": raw_item.get("reasonCode", "NO_VERIFIABLE_EVIDENCE"),
                "evidenceFields": raw_item.get("evidenceFields", []),
            }
        )
    return {
        **{key: value for key, value in payload.items() if key != "ranked"},
        "evaluations": evaluations,
    }


def _record_loadtest_usage(tier: str) -> None:
    """#438 D6 — 트레이스에 스텁 모델 id 를 기록한다. 토큰 수는 **모른다(None)** 로 둔다.

    실제 vendor 호출이 없으니 0 으로 추정하면 "토큰 0개짜리 진짜 호출"과 구별이 안 된다
    (#151 정직성 규약) — `AnthropicLLM`/`OpenAILLM` 의 `_record_usage` 와 같은 기록 경로
    (`app/core/tracing.py::RequestTrace.record_llm_usage`)를 그대로 쓴다.
    """
    model = LOADTEST_MODEL_IDS.get(tier, LOADTEST_MODEL_IDS["fast"])
    if trace := current_request_trace():
        trace.record_llm_usage(model=model, prompt_tokens=None, completion_tokens=None)


class LoadTestLLM(ScriptedLLM):
    """`LLM_PROVIDER=scripted` 가 실제로 뜨는 서버에서 쓰는 결정론 스텁(#438).

    동일 입력 → 동일 출력(난수·시간·전역 상태 금지). ``ScriptedLLM`` 을 상속해 enrich·delta·
    consolidate 는 그 DEFAULT_* 를 그대로 쓰고(§D3), 구매자 레인 5종(decompose·rerank·
    category_select·category_scope·need_priority·needs_expansion)은 프롬프트에서 응답을
    유도한다. 판매자 레인은 이 클래스가 아니라 ``app/agents/seller/models.py`` 의 명시적
    거부로 막는다(#438 D5 — 판매자 스텁은 범위 밖).
    """

    def __init__(
        self,
        *,
        mode: ScriptedLLMMode = "instant",
        delay_s: float = 5.0,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.delay_s = delay_s
        self._delay_task: asyncio.Task[None] | None = None

    async def _apply_delay_once(self) -> None:
        """delayed 프로파일의 I/O 대기를 인스턴스당 정확히 한 번 적용한다.

        buyer 요청은 같은 인스턴스로 decompose·rerank 등 여러 호출을 하므로 호출마다 지연하면
        사용자 실측 5초가 호출 수만큼 부풀어 오른다. 동시에 진입한 호출은 같은 sleep task를
        기다리므로 한 호출만 지연을 건너뛰는 일도 없다. ``asyncio.sleep``이라 이벤트 루프는
        막지 않는다.
        """
        if self.mode != "delayed" or self.delay_s <= 0:
            return
        if self._delay_task is None:
            self._delay_task = asyncio.create_task(asyncio.sleep(self.delay_s))
        # 같은 요청에서 호출이 겹쳐도 모두 한 대기를 공유한다. 한 waiter의 취소가 공유 sleep을
        # 취소해 다른 호출이 지연을 건너뛰지 않도록 shield한다.
        await asyncio.shield(self._delay_task)

    @staticmethod
    def _extract_after(user: str, marker: str) -> str | None:
        """``user`` 프롬프트에서 ``marker`` 뒤 첫 줄(개행 전까지, 없으면 끝까지)을 뗀다."""
        idx = user.find(marker)
        if idx == -1:
            return None
        rest = user[idx + len(marker) :]
        end = rest.find("\n")
        return rest if end == -1 else rest[:end]

    @classmethod
    def _classify_loadtest(cls, system: str) -> str | None:
        """§D3 마커 우선순위로 호출 종류를 판정한다. 미상이면 None(=조용한 폴백 금지, 호출측이 던진다)."""
        if _ENRICH_MARK in system:
            return "enrich"
        if _DELTA_MARK in system:
            return "delta"
        if _CONSOLIDATE_MARK in system:
            return "consolidate"
        if _CATEGORY_SCOPE_MARK in system:
            return "category_scope"
        if _NEED_PRIORITY_MARK in system:
            return "need_priority"
        if _NEEDS_EXPANSION_MARK in system:
            return "needs_expansion"
        if _DECOMPOSE_MARK in system:
            return "decompose"
        if _RERANK_SCORING_MARK in system:
            return "rerank"
        if _RERANK_MARK in system:
            return "rerank"
        if _CATEGORY_SELECT_MARK in system:
            return "category_select"
        return None

    def _decompose_response(self, user: str) -> dict:
        """스키마상 유효한 최소 decompose 응답 — semanticQuery 는 발화에서 유도, 하드필터는 비운다.

        priceMax 등 하드필터를 실으면 실 카탈로그가 0건이 되어 rerank 없이 degrade 로 떨어진다
        (#438 D3 핵심 설계 제약) — 그래서 filters 는 항상 빈 객체다.

        카테고리 leg 를 발화가 있으면 **항상 한 건** 싣는다(#438 R2 F1) — 이슈 본문이 이름까지
        찍어 명시한 측정 대상 `category_search_pool_max_size` 커넥션 풀은 카테고리 매핑의 앵커
        조회(leg 당 raw·query 2건 동시)에만 쓰인다. `categoryQueries` 를 항상 비우면
        `_parse_category_queries` 가 leg 0건을 내 그 풀에 요청이 한 건도 안 가고, 이슈가 재려던
        병목 하나가 측정에서 통째로 빠진다. leg 를 실으면 `case` 도 1("상품명이 발화에 있음")로
        맞춘다 — `filters` 가 빈 것과 의미가 어긋나지 않게. 매핑 자체는 실 임베딩 사전이 하므로
        카탈로그가 고정이면 결정론이 유지되고, 매핑 실패는 `category_unmapped` 로 보고서에
        드러난다 — 조용히 레인을 건너뛰는 것보다 낫다.
        """
        utterance = (self._extract_after(user, _USER_MESSAGE_MARKER) or "").strip()
        category_queries = [{"category": utterance, "query": utterance}] if utterance else []
        return {
            "intent": "recommend",
            "reply": "",
            "case": 1 if utterance else 2,
            "semanticQuery": utterance,
            "categoryQueries": category_queries,
            "filters": {},
        }

    def _rerank_response(self, user: str, *, scored: bool) -> dict:
        """CANDIDATES 의 productId 를 등장 순서대로 되돌린다 — 실 카탈로그에서도 정상 경로를 유지한다."""
        raw_candidates = self._extract_after(user, _CANDIDATES_MARKER)
        candidates: list = []
        if raw_candidates is not None:
            try:
                parsed = json.loads(raw_candidates)
            except (ValueError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                candidates = parsed
        ranked = [
            {"productId": item["productId"], "rationale": "부하 테스트 스텁 고정 근거"}
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("productId"), int)
        ]
        payload = {"ranked": ranked, "overallComment": "부하 테스트 스텁 응답"}
        return _scored_rerank_payload(payload) if scored else payload

    def _category_select_response(self, user: str) -> dict:
        """CANDIDATES 목록의 **첫 후보**를 결정론적으로 고른다(#438 R2 F2).

        `select_category` 는 후보에 정확히 있는 값만 채택하고 그 외(환각·null)는 호출부가
        임베딩 top-1 폴백으로 퇴화시킨다. 항상 null 을 내면 그 폴백 갈래만 영원히 타 매핑
        수락 경로 자체가 부하 측정에서 빠진다 — rerank 에서 잡았던 것과 같은 유형의 결함이다.
        후보 목록을 찾지 못했을 때만 null 로 남긴다.
        """
        idx = user.find(_CATEGORY_CANDIDATES_MARKER)
        if idx != -1:
            rest = user[idx + len(_CATEGORY_CANDIDATES_MARKER) :]
            for line in rest.splitlines():
                if line.startswith("- ") and line[2:].strip():
                    return {"category": line[2:].strip()}
        return {"category": None}

    def _need_priority_response(self, user: str) -> dict:
        """NEEDS 배열과 같은 길이로 전부 2(권장)를 채운다 — 파서가 요구하는 길이 정합만 지킨다."""
        raw_needs = self._extract_after(user, _NEEDS_MARKER)
        needs: list = []
        if raw_needs is not None:
            try:
                parsed = json.loads(raw_needs)
            except (ValueError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                needs = parsed
        return {"priorities": [2] * len(needs)}

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        kind = self._classify_loadtest(system)
        if kind is not None:
            await self._apply_delay_once()
        self.calls.append((kind or "unknown", tier))
        self.complete_kwargs.append((kind or "unknown", max_tokens, reasoning_effort))
        if kind == "enrich":
            response = json.dumps(self._enrich, ensure_ascii=False)
        elif kind == "delta":
            response = json.dumps(self._delta, ensure_ascii=False)
        elif kind == "consolidate":
            response = self._profile_markdown
        elif kind == "decompose":
            response = json.dumps(self._decompose_response(user), ensure_ascii=False)
        elif kind == "rerank":
            response = json.dumps(
                self._rerank_response(user, scored=_RERANK_SCORING_MARK in system),
                ensure_ascii=False,
            )
        elif kind == "category_select":
            response = json.dumps(self._category_select_response(user), ensure_ascii=False)
        elif kind == "category_scope":
            response = json.dumps({"scopeFree": False}, ensure_ascii=False)
        elif kind == "need_priority":
            response = json.dumps(self._need_priority_response(user), ensure_ascii=False)
        elif kind == "needs_expansion":
            response = json.dumps(
                {"items": ["여행용 파우치", "세면도구 파우치"]}, ensure_ascii=False
            )
        else:
            # 조용한 폴백 금지(#438 D3) — 새 호출 지점이 마커 없이 흘러들면 여기서 소리 나게
            # 실패시켜 벤치마크 결과에 error 로 드러나게 한다. 응답이 없으니 usage 도 기록하지
            # 않는다(실패한 실제 provider 호출이 토큰을 기록하지 않는 것과 같은 규약).
            raise LLMError(
                "LoadTestLLM: 미상 system 프롬프트 — 이 호출 지점의 응답 규칙이 "
                "app/core/llm_scripted.py::LoadTestLLM 에 없다. 조용히 폴백하면 이 레인만 "
                "degrade 로 새어 부하 측정이 거짓말이 된다(#438 D3) — 마커/핸들러를 추가할 것."
            )
        _record_loadtest_usage(tier)
        return response
