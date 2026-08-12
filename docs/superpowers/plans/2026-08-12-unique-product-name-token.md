# Unique Product Name Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 추천 목록에서 사용자가 말한 상품명 토큰이 한 상품에만 속할 때 LLM의 다른 상품 선택을 안전하게 교정한다.

**Architecture:** 추천 카드 전용 결정론 해소기 안에 NFKC + casefold 기반 토큰 매칭을 추가한다. 정확한 토큰 경계와 추천 표면 내 문서 빈도 1을 요구하고, 여러 상품이 지목되거나 부정 표현이 있으면 기존 LLM 경로에 양보한다.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, Ruff, pre-commit

## Global Constraints

- 새 외부 의존성을 추가하지 않는다.
- 추천 카드 표면(`name_confirmation_enabled=True`)에서만 유일 토큰 확정을 적용한다.
- 숫자 전용·1글자·공통·담기 명령·장바구니 문맥 토큰은 확정 근거로 쓰지 않는다.
- 불확실하거나 서로 다른 상품이 동시에 지목되면 LLM 결과에 양보한다.
- 구현 커밋 전에 관련 pre-commit 검사를 실행한다.

---

### Task 1: 추천 카드 유일 이름 토큰 해소

**Files:**
- Modify: `app/agents/buyer/screen_reference.py:238-333`
- Test: `tests/unit/test_screen_context.py:2479-2735`

**Interfaces:**
- Consumes: `resolve_screen_reference(message, products=..., name_confirmation_enabled=True, ...)`
- Produces: `_unique_product_name_token_match(message: str, products: Sequence[tuple[int, str]]) -> int | None`
- Produces: `ScreenResolution(product_id=<unique id>, reason="screen_unique_name_token_match")`

- [x] **Step 1: 운영 재현 통합 테스트 작성**

```python
async def test_reco_card_unique_name_token_overrides_wrong_llm_product(monkeypatch):
    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="Septwolves 지갑 담아줘", threadId="t-reco-unique-token")
    )
    await cart_store.set_last_reco(
        key,
        [
            (5644, "W06 남성지갑 명품 천연가죽 남자 반지갑 카드 학생 septwolves"),
            (5695, "구찌 썸머블프여성 GG 마몬트 지퍼 장지갑"),
        ],
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 5695}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert added["product_id"] == 5644
```

- [x] **Step 2: 안전 경계 단위 테스트 작성**

```python
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Septwolves 지갑 담아줘", 5644),
        ("지갑 담아줘", None),
        ("Septwolves 구찌 지갑 담아줘", None),
        ("wolf 지갑 담아줘", None),
        ("Septwolves 말고 구찌 지갑 담아줘", None),
    ],
)
def test_reco_card_unique_name_token_safety_boundaries(message, expected):
    resolved = resolve_screen_reference(message, **reco_surface_args)
    assert (resolved.product_id if resolved else None) == expected
```

- [x] **Step 3: 테스트가 현재 구현에서 올바르게 실패하는지 확인**

Run:

```bash
uv run pytest -q tests/unit/test_screen_context.py \
  -k 'unique_name_token_overrides_wrong_llm_product or unique_name_token_safety_boundaries'
```

Expected: `Septwolves` 재현이 LLM의 `5695`를 그대로 사용하거나 해소 결과 `None`이라 FAIL.

- [x] **Step 4: 최소 구현 추가**

```python
import unicodedata
from collections import Counter

_NAME_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _name_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return {
        token
        for token in _NAME_TOKEN.findall(normalized)
        if len(token) >= 2 and any(char.isalpha() for char in token)
    }


def _message_name_tokens(message: str) -> set[str]:
    return {
        token
        for token in _name_tokens(message)
        if token not in _CART_CONTEXT_TOKENS
        and not token.startswith(_CART_ACTION_TOKEN_PREFIXES)
    }


def _unique_product_name_token_match(
    message: str, products: Sequence[tuple[int, str]]
) -> int | None:
    product_tokens = [(pid, _name_tokens(name)) for pid, name in products]
    frequency = Counter(token for _, tokens in product_tokens for token in tokens)
    message_tokens = _message_name_tokens(message)
    matches = {
        pid
        for pid, tokens in product_tokens
        if any(token in message_tokens and frequency[token] == 1 for token in tokens)
    }
    return next(iter(matches)) if len(matches) == 1 else None
```

`resolve_screen_reference`의 기존 부정 게이트 안에서 전체 이름 규칙 다음에 이 헬퍼를 호출하고,
ID가 있으면 `screen_unique_name_token_match` 사유로 반환한다.

- [x] **Step 5: 대상 테스트 green 확인**

Run:

```bash
uv run pytest -q tests/unit/test_screen_context.py \
  -k 'unique_name_token or reco_card_full_name_mention_is_confirmed_by_code or reco_card_ambiguous_name_match_defers_to_llm or reco_resolution_is_always_within_allowed_and_products'
```

Expected: 전부 PASS.

- [x] **Step 6: 관련 전체 파일과 정적 검사 실행**

Run:

```bash
uv run pytest -q tests/unit/test_screen_context.py
uv run ruff check app/agents/buyer/screen_reference.py tests/unit/test_screen_context.py
uv run ruff format --check app/agents/buyer/screen_reference.py tests/unit/test_screen_context.py
uv run pre-commit run --files app/agents/buyer/screen_reference.py tests/unit/test_screen_context.py \
  docs/superpowers/specs/2026-08-12-unique-product-name-token-design.md \
  docs/superpowers/plans/2026-08-12-unique-product-name-token.md
```

Expected: 모든 명령 exit 0.

- [x] **Step 7: 전체 테스트와 커밋**

Run:

```bash
uv run pytest -q
git diff --check
git status --short
```

검증 후 의도한 파일만 명시적으로 stage하고 Lore trailer를 포함한 Conventional Commit으로
커밋한다.

실행 결과: `test_screen_context.py` 156건과 변경 파일 Ruff/pre-commit은 통과했다. 전체 스위트는
판매자 인증 테스트 1건이 baseline에서도 정지해 해당 파일을 제외하고 재실행했으며, 6960건 통과
후 변경 파일 밖 판매자 분석 DB 경로의 baseline 실패 2건을 확인했다. 이 환경 격차는 PR에
명시한다.
