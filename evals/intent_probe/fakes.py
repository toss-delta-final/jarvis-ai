"""API 를 부르지 않는 결정론 가짜 LLM — `--dry-run` 과 유닛테스트 전용.

실 분포를 흉내 내지 않는다. **배관이 도는지**(페이서·N 채우기·채점·산출물)만 확인하는 물건이라
정답과 오답을 고정 패턴으로 낸다. 이 가짜로 잰 표를 프롬프트 판정 근거로 쓰면 안 된다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from app.agents.buyer.recommendation.category_scope import _SYSTEM as _SCOPE_SYSTEM
from app.agents.buyer.recommendation.underspecified_classifier import (
    _SYSTEM as _UNDERSPECIFIED_SYSTEM,
)
from app.core.llm import LLMError
from evals.intent_probe.schema import (
    AnchorSet,
    CATEGORY_ACTION_GROUP,
    CATEGORY_ACTIONS,
    ScreenFixture,
    SCREEN_GROUP,
    Utterance,
)

_USER_MESSAGE_PREFIX = "USER_MESSAGE: "
_PENDING_PREFIX = "PENDING_CART: "
_SCREEN_PREFIX = "SCREEN: "
_SCREEN_PRODUCT_ID_RE = re.compile(r'"productId":\s*(\d+)')
# 오답 주기가 고를 다음 값 — carry→clear→replace→carry 로 순환한다(결정론).
_NEXT_CATEGORY_ACTION = {
    action: CATEGORY_ACTIONS[(index + 1) % len(CATEGORY_ACTIONS)]
    for index, action in enumerate(CATEGORY_ACTIONS)
}


def _field(user: str, prefix: str) -> str:
    for line in user.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


class ScriptedDecomposeLLM:
    """앵커의 정답을 되돌려주되 고정 주기로 오답을 섞는 가짜 클라이언트.

    `reaskProductListPosition` 에 **일부러 민감**하다 — `--fixture a` 와 `--fixture b` 의
    산출물이 달라진다는 사실 자체를 API 없이 시연하기 위해서다(#260 수용 기준).
    """

    def __init__(
        self,
        anchors: AnchorSet,
        *,
        fail_first: int = 0,
        always_fail: bool = False,
        wrong_every: int = 7,
    ) -> None:
        self.anchors = anchors
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.wrong_every = wrong_every
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._per_cell: Counter[str] = Counter()
        self._per_scope_cell: Counter[str] = Counter()
        # [#300, §4.5] `screen-001`/`screen-002` 는 텍스트가 같다("이거 담아줘") — 텍스트 하나로
        # 발화를 특정할 수 없는 경우를 **유일한 매핑**과 **모호한 매핑**으로 갈라 둔다. 텍스트가
        # 유일하면 기존과 같은 O(1) 조회, 겹치면 user 메시지의 `SCREEN:` 줄(productId 신호)로
        # 가른다(`_resolve_ambiguous_text`). 새 중복 텍스트가 또 생기면(그리고 SCREEN 신호로도
        # 못 가르면) 조용히 깨지지 않도록 `AssertionError` 로 드러낸다.
        text_counts = Counter(utterance.text for utterance in anchors.utterances)
        self._by_text: dict[str, Utterance] = {}
        self._ambiguous_by_text: dict[str, list[Utterance]] = {}
        for utterance in anchors.utterances:
            if text_counts[utterance.text] == 1:
                self._by_text[utterance.text] = utterance
            else:
                self._ambiguous_by_text.setdefault(utterance.text, []).append(utterance)
        context_by_id = {context.context_id: context for context in anchors.contexts}
        screen_by_id = {screen.screen_id: screen for screen in anchors.screens}
        self._screen_by_utterance_id: dict[str, ScreenFixture] = {}
        for utterance in anchors.utterances:
            if utterance.group != SCREEN_GROUP:
                continue
            screen_ref = context_by_id[utterance.contexts[0]].screen_ref
            assert screen_ref is not None  # 스키마 검증자가 이미 보장한다
            self._screen_by_utterance_id[utterance.utterance_id] = screen_by_id[screen_ref]
        prior = anchors.category_prior_filters
        self._prior_category = str(prior.get("category") or "")
        self._prior_semantic = str(prior.get("semanticQuery") or "")

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        self.calls.append({"system": system, "user": user, "tier": tier, "maxTokens": max_tokens})
        self._attempts += 1
        if self.always_fail or self._attempts <= self.fail_first:
            raise LLMError("scripted failure")
        # [#84] 배포 경로는 같은 클라이언트로 **두 종류**의 호출을 한다(decompose · 범위 해제
        # 분류기). tier 가 둘 다 fast 라 tier 로는 갈리지 않으므로 system 프롬프트로 가른다 —
        # 이렇게 해야 `--dry-run` 이 분류기 배관까지 실제로 태운다.
        if system == _SCOPE_SYSTEM:
            return json.dumps(self._scope_answer(user), ensure_ascii=False)
        if system == _UNDERSPECIFIED_SYSTEM:
            # dry-run은 품질 판단이 아니라 배관 확인용이다. 보수적 false로 원 decompose 응답을
            # 유지해 기존 결정론 패턴을 보존한다.
            return '{"underspecified": false}'
        return json.dumps(self._answer(user), ensure_ascii=False)

    def _scope_answer(self, user: str) -> dict[str, Any]:
        """분류기 응답 — 기대 categoryAction 이 `clear` 면 true, 아니면 false.

        분류기 호출은 셀마다 표본당 1회라 `_per_cell` 카운터를 decompose 와 **따로** 센다
        (같이 세면 오답 주기가 두 배로 빨라져 dry-run 산출물의 뜻이 달라진다).
        """
        text = user.rsplit("사용자 발화: ", 1)[-1]
        utterance = self._by_text.get(text)
        if utterance is None:
            return {"scopeFree": False}
        index = self._per_scope_cell[user]
        self._per_scope_cell[user] += 1
        wrong = index % self.wrong_every == self.wrong_every - 1
        scope_free = utterance.expected.category_action == "clear"
        return {"scopeFree": (not scope_free) if wrong else scope_free}

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        yield await self.complete(system=system, user=user, tier=tier, max_tokens=max_tokens)

    def _resolve_ambiguous_text(self, text: str, user: str) -> Utterance:
        """[#300, §4.5] 텍스트가 여러 발화와 겹칠 때 `SCREEN:` 줄(productId 신호)로 가른다.

        `_by_text` 는 텍스트로만 찾으므로 `screen-001`/`screen-002` 처럼 텍스트가 같은 두 발화가
        있으면 나중 것이 앞엣것을 조용히 덮는다 — dry-run 이 조용히 틀린 값을 낸다. 후보들의
        screen 픽스처(productId 집합)와 user 메시지의 `SCREEN.상품[].productId` 집합을 대조해
        가른다. 새 중복 텍스트가 생기고 이 신호로도 못 가르면(SCREEN 이 없거나 두 후보의 id
        집합이 같으면) **조용히 깨지지 않도록** 여기서 드러낸다.
        """
        candidates = self._ambiguous_by_text[text]
        screen_line = _field(user, _SCREEN_PREFIX)
        ids_in_line = frozenset(int(m) for m in _SCREEN_PRODUCT_ID_RE.findall(screen_line))
        for candidate in candidates:
            screen = self._screen_by_utterance_id.get(candidate.utterance_id)
            if screen is not None and {p.product_id for p in screen.products} == ids_in_line:
                return candidate
        raise AssertionError(
            f"ScriptedDecomposeLLM: 텍스트 {text!r} 가 여러 발화와 겹치는데 SCREEN 신호로도 "
            f"특정하지 못했습니다 — candidates={[c.utterance_id for c in candidates]}, "
            f"idsInLine={sorted(ids_in_line)}"
        )

    def _screen_answer(self, utterance: Utterance, *, wrong: bool) -> dict[str, Any]:
        """[#300, §4.5] screen 셀에서 **원본 decompose 산출**(해소기 통과 전)을 흉내낸다.

        해소기(`resolve_screen_reference`)는 러너가 태우므로 여기서는 재구현하지 않는다.
        `screenExact`(001·003·004·005)와 `screenReask`/`screenNotHallucinated`(002·006)는
        고정 주기 오답을 유지한다 — 그래야 dry-run 이 `screenPromptLayerHitCount`(해소기 전
        원본 산출만으로 규칙을 만족한 표본)와 override 경로를 실제로 태운다. screen-001·002·
        003·004·006 은 해소기가 텍스트만으로 항상 결정적으로 개입하므로(이거=단일 후보 확정/
        여러 후보 되물음, 순번·좌표, 두 목록 밖 id 차단) 여기서 낸 원본 값과 무관하게 최종
        `resolvedProductId` 는 같아진다 — screen-005(이름 매칭)만 해소기가 개입하지 않아
        (`resolve_screen_reference` 의 양보 (B)) 이 원본 산출이 곧 최종값이다.
        """
        screen = self._screen_by_utterance_id[utterance.utterance_id]
        screen_ids = [product.product_id for product in screen.products]
        rule = utterance.expected.product_id_rule
        if rule == "screenExact":
            correct = utterance.expected.product_id
            if not wrong:
                return self._envelope("cart_add", product_id=correct)
            alt = next((pid for pid in screen_ids if pid != correct), correct)
            return self._envelope("cart_add", product_id=alt)
        # screenReask · screenNotHallucinated — 정답은 "확정하지 않는다"(productId=null).
        # 원본 스크립트가 실측한 실패 모양(임의 확정)을 흉내내려고, 주기적으로 화면의 첫 상품을
        # 잘못 확정하는 오답을 섞는다.
        if not wrong:
            return self._envelope("cart_add", product_id=None)
        guess = screen_ids[0] if screen_ids else None
        return self._envelope("cart_add", product_id=guess)

    def _answer(self, user: str) -> dict[str, Any]:
        text = _field(user, _USER_MESSAGE_PREFIX)
        # 카운터는 **user 메시지 전체**로 나눈다 — 컨텍스트별로 실려 나가는 줄이 다르므로 셀마다
        # 고유하다. 발화+되물음여부로만 나누면 `맥락 없음`과 `직전 추천` 셀이 카운터를 공유해
        # 오답 주기가 셀 간에 섞이고, 그 순서는 동시성 스케줄링에 달린다 — dry-run 산출물의
        # 결정론이 운에 걸린다. run_cell 은 셀 안에서 순차 호출하므로 이 키면 항상 결정론이다.
        index = self._per_cell[user]
        self._per_cell[user] += 1
        utterance = self._by_text.get(text)
        if utterance is None and text in self._ambiguous_by_text:
            utterance = self._resolve_ambiguous_text(text, user)
        if utterance is None:
            return self._envelope("general")
        wrong = index % self.wrong_every == self.wrong_every - 1
        expected = utterance.expected

        if utterance.group == SCREEN_GROUP:
            return self._screen_answer(utterance, wrong=wrong)

        if utterance.group == "option_answer":
            option_id = expected.option_id
            # 되물음 상품이 목록 1번이 아니면 옵션 선택이 흔들린다 — #240 실측을 모사한 결정론 규칙.
            slipped = self.anchors.reask_product_list_position != 1 and index % 2 == 1
            if wrong or slipped:
                others = [o.option_id for o in self.anchors.options if o.option_id != option_id]
                option_id = others[0] if others else option_id
            return self._envelope(
                "cart_add", product_id=self.anchors.reask_product_id, option_id=option_id
            )

        if utterance.group == "switch":
            target = self._switch_target(text)
            product_id = self.anchors.reask_product_id if wrong else target
            return self._envelope("cart_add", product_id=product_id)

        if utterance.group == CATEGORY_ACTION_GROUP:
            action = expected.category_action or "carry"
            if wrong:
                # 오답도 3종 안에서 돈다 — carry→clear 가 섞이므로 dry-run 이
                # `categoryClearOnRefineCount`(리파인이 풀린 표본) 배관까지 실제로 태운다.
                action = _NEXT_CATEGORY_ACTION[action]
            # replace 는 **leg 를 실어야** 확정값이 replace 가 된다(신호 없는 replace 는
            # `resolve_category_action` 규칙 3 으로 carry 가 된다) — 실 LLM 도 같은 모양을 낸다.
            #
            # [2차 리뷰 P2] carry·clear 에는 **직전 카테고리를 복사한 leg** 를 싣는다. 실 LLM 이
            # 그렇게 낸다(`_SYSTEM` 의 categoryQueries 불릿이 지시한다 — 리셋 발화의 30~31/32).
            # 가짜가 빈 배열을 내면 그 충돌(에코 leg 가 확정값을 replace 로 만든다)이 가려져
            # dry-run 이 `categoryLegsEchoPrior` 보정 경로를 한 번도 태우지 않는다.
            if action == "replace":
                queries = [{"category": None, "query": "새 상품"}]
            else:
                queries = [{"category": self._prior_category, "query": self._prior_semantic}]
            return self._envelope("recommend", category_queries=queries)

        if wrong:
            fallback = "general" if expected.intent == "recommend" else "recommend"
            return self._envelope(fallback)
        if expected.intent == "cart_add":
            return self._envelope("cart_add", product_id=self.anchors.reask_product_id)
        return self._envelope(expected.intent)

    def _switch_target(self, text: str) -> int:
        for product in self.anchors.last_recommendations:
            if product.product_id == self.anchors.reask_product_id:
                continue
            if any(
                target in text and target in product.name for target in self.anchors.switch_targets
            ):
                return product.product_id
        return next(
            product.product_id
            for product in self.anchors.last_recommendations
            if product.product_id != self.anchors.reask_product_id
        )

    def _envelope(
        self,
        intent: str,
        *,
        product_id: int | None = None,
        option_id: int | None = None,
        category_queries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "reply": "네" if intent == "general" else "",
            "case": 2,
            "semanticQuery": "세탁 세제",
            "filters": {},
            "cart": {"productId": product_id, "optionId": option_id, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
            "categoryQueries": category_queries or [],
            "buyAll": False,
            "totalBudget": None,
            "scopedToPrevious": False,
        }
