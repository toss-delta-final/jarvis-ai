# Buyer Cart Option Re-ask Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the five buyer cart option re-ask outputs as one-based ordered Markdown rows whose complete existing labels are bold, while preserving every #572 behavior outside those rows.

**Architecture:** Add one presentation-only `_numbered_option_rows(labels: Sequence[str]) -> str` helper beside `_option_label` in `app/agents/buyer/cart/graph.py`. Feed it only completed, already-sanitized non-empty labels from `_options_text` and the I-1 hint fallback, leaving option selection, sanitization, surcharge construction, lead/tail assembly, summaries, and degradation branches intact.

**Tech Stack:** Python 3.12, Pydantic `CartOption`, pytest/pytest-asyncio, Ruff, Markdown rendered through buyer `token.text`.

## Global Constraints

- Render every actual option row in all five buyer cart option re-ask paths as `<one-based index>. **<existing display label>**`.
- Keep additional-price text inside the bold label without changing its existing spelling or spacing, for example `2. **화이트 / L(+1,000원)**`.
- Make the change at the smallest shared formatting boundary used by those five paths.
- Preserve all behavior established by #572 except the option-row Markdown decoration.
- The formatter must reuse the existing option-label result without re-parsing or reconstructing it.
- Numbering is one-based and follows the existing option order.
- Lead lines, option lines, and tail lines retain the current newline boundaries and ordering.
- Option names retain their existing raw-name semantics after the current sanitization step.
- No links, HTML, nested Markdown, or additional Markdown forms are introduced.
- If `_options_text` receives no displayable `CartOption` labels, preserve its current fallback text `옵션` exactly; do not turn that fallback word into a numbered/bold actual-option row.
- If `CART_OPTION_REQUIRED` supplies no I-2 options, preserve the current branch behavior: use sanitized I-1 hint names when present, otherwise emit the existing sold-out degradation sentence. If sanitization removes every hint name, follow that same sold-out degradation path.
- If an option is removed or normalized by current sanitization, preserve that outcome before Markdown formatting.
- Preserve current handling of missing, malformed, or unusual option fields. This issue does not add fallback labels or validation policy.
- Do not escape or reinterpret raw option-name content beyond existing sanitization.
- In the hint fallback, actual names are numbered and bolded while `외 N개` remains an unnumbered, non-bold summary line.
- Scope is exactly five buyer-facing cart outputs: #455 condition-narrowed `CART_OPTION_REQUIRED`, #454 unmet-color-condition `CART_OPTION_REQUIRED`, default `CART_OPTION_REQUIRED`, `CART_OPTION_INVALID`, and the empty-I-2-list fallback using sanitized I-1 hint names.
- Exclude shopper-reply parsing or `optionId` mapping, recommendation `screen.columns`, recommendation cards, frontend code, seller flows/messages, HTML, links, any Markdown beyond ordered-list prefixes and bold labels, and all new sanitization, escaping, fallback naming, sorting, pricing, or validation semantics.
- Strict TDD order is mandatory: edit the tests and observe the expected RED failures before editing production code.
- No new dependencies and no broad refactor.

---

### Task 1: Implement the shared formatter through one complete RED→GREEN cycle

**Files:**
- Modify: `tests/unit/test_cart.py:13-19,196-224,3523-3734`
- Modify: `app/agents/buyer/cart/graph.py:62-100,128-200`

**Interfaces:**
- Consumes: existing `CartOption`, `OptionHint`, `_option_label(option: CartOption) -> str`, `_strip_unsafe(text: str) -> str`, `_options_text(options: list[CartOption]) -> str`, `stream_cart_add(...)`, `_run_add(...)`, `_collect(...)`, already-filtered `Sequence[str]` labels, and the `app.agents.buyer.cart.graph` module object used to probe the missing helper at test runtime without breaking collection.
- Produces: `_numbered_option_rows(labels: Sequence[str]) -> str`; exact literal expectations for all five affected outputs; `_options_text(options: list[CartOption]) -> str` retains its signature and empty fallback; `_options_prompt(lead: str, options: list[CartOption], tail: str = "") -> str` retains its signature.

This combined task owns one complete RED→GREEN cycle and ends with one independently reviewable implementation commit containing both the failing-first tests and their minimal production implementation.

- [ ] **Step 1: Import the graph module without importing the missing helper directly**

Add the module alias immediately above the existing cart-graph function import, and keep the existing function imports unchanged:

```python
from app.agents.buyer.cart import graph as cart_graph
from app.agents.buyer.cart.graph import (
    _options_prompt,
    _options_text,
    stream_cart_add,
    stream_cart_view,
)
```

Do not add `_numbered_option_rows` to the direct import list: the module alias lets pytest collect and execute every selected regression while the production helper is still absent.

- [ ] **Step 2: Add focused formatter contract tests before changing production**

Insert immediately above the `# ─────────── 이슈 #570` regression section:

```python
def test_numbered_option_rows_bolds_complete_labels_in_order() -> None:
    labels = ["블랙 / M", "화이트 / L(+1,000원)"]

    assert cart_graph._numbered_option_rows(labels) == (
        "1. **블랙 / M**\n"
        "2. **화이트 / L(+1,000원)**"
    )


def test_options_text_numbers_only_sanitized_displayable_labels_contiguously() -> None:
    options = [
        CartOption(option_id=1, name="블\x1b[31m랙\u200b"),
        CartOption(option_id=2, name="\u200b\u202e"),
        CartOption(option_id=3, name="화\n이트"),
    ]

    assert _options_text(options) == "1. **블[31m랙**\n2. **화 이트**"
```

These tests deliberately establish that numbering happens after the existing `_strip_unsafe` filtering/normalization and that the surcharge is already part of the label before the bold wrapper is added.

Do not add a temporary production stub, alias, or empty implementation for `_numbered_option_rows` before completing the RED run in Step 5. The missing runtime attribute and the old literal output are both required failure evidence.

- [ ] **Step 3: Replace the five #570 literal expectations with exact #582 output strings**

Keep each fixture, lead, tail, and branch setup unchanged. Replace only the literal assertions and update each docstring issue reference to `#582`:

```python
# test_cart_option_narrow_reask_literal_matches_issue_570
assert token == (
    "말씀하신 조건에 맞는 옵션이에요:\n"
    "1. **블랙 / M**\n"
    "2. **화이트 / M**\n"
    "이 중에서 고르시거나 다른 옵션을 말씀해 주세요."
)

# test_cart_option_color_unmet_reask_literal_matches_issue_570
assert token == (
    "'빨강' 조건에 맞는 옵션은 찾지 못했어요. 고를 수 있는 옵션은 이거예요:\n"
    "1. **블랙 / M**\n"
    "2. **화이트 / M**\n"
    "이 중에서 고르시거나 다른 상품을 말씀해 주세요."
)

# test_cart_option_default_reask_literal_matches_issue_570
assert token == (
    "옵션을 선택해 주세요:\n"
    "1. **블랙 / M**\n"
    "2. **화이트 / M**\n"
    "어떤 걸로 담을까요?"
)

# test_cart_option_invalid_reask_literal_matches_issue_570
assert token == (
    "그 옵션을 찾지 못했어요. 다시 골라 주세요:\n"
    "1. **블랙 / M**\n"
    "2. **화이트 / M**"
)

# test_cart_option_hint_fallback_literal_matches_issue_570
assert token == (
    "옵션을 선택해 주세요:\n"
    "1. **블랙**\n"
    "2. **화이트**\n"
    "3. **레드**\n"
    "외 2개\n"
    "어떤 걸로 담을까요?"
)
```

Do not calculate any of these five expectations with `_options_text`, `_options_prompt`, or `_numbered_option_rows`; they must remain independent literal regressions. Keep the existing test function names to avoid unnecessary test-selection churn.

- [ ] **Step 4: Update adjacent preservation regressions with literal expectations**

Change only the assertions in the existing tests shown below:

```python
# test_cart_option_reask_strips_seller_text
assert token == (
    "옵션을 선택해 주세요:\n"
    "1. **블[31m루**\n"
    "2. **레 드**\n"
    "어떤 걸로 담을까요?"
)
assert all(ch not in token for ch in ("\x1b", "\u200b", "\u202e"))

# test_cart_option_hint_fallback_without_total_has_no_extra_line
assert token == (
    "옵션을 선택해 주세요:\n"
    "1. **블랙**\n"
    "2. **화이트**\n"
    "어떤 걸로 담을까요?"
)

# test_cart_option_reask_reproduces_issue_570_symptom
lines = token.split("\n")
option_lines = lines[1:-1]
assert option_lines == ["1. **블랙 / M**", "2. **화이트 / M**"]
assert all(not line.endswith(".") for line in option_lines)

# test_cart_add_reask_surcharge_option_on_own_line
assert token == (
    "옵션을 선택해 주세요:\n"
    "1. **블루**\n"
    "2. **레드(+1,000원)**\n"
    "어떤 걸로 담을까요?"
)

# test_options_text_empty_list_falls_back_to_default_label remains byte-identical
assert _options_text([]) == "옵션"

# test_cart_option_numeric_prefix_name_not_escaped
assert "\\" not in token
assert "1. **4. 얼큰한맛 92g x 30개**" in token.split("\n")
```

The numeric-prefix assertion intentionally proves raw option-name content is neither escaped nor reinterpreted; the outer `1. **...**` is presentation added around the unchanged name.

- [ ] **Step 5: Run the focused tests and observe RED before any production edit**

Run:

```bash
uv run pytest \
  tests/unit/test_cart.py::test_numbered_option_rows_bolds_complete_labels_in_order \
  tests/unit/test_cart.py::test_options_text_numbers_only_sanitized_displayable_labels_contiguously \
  tests/unit/test_cart.py::test_cart_option_narrow_reask_literal_matches_issue_570 \
  tests/unit/test_cart.py::test_cart_option_color_unmet_reask_literal_matches_issue_570 \
  tests/unit/test_cart.py::test_cart_option_default_reask_literal_matches_issue_570 \
  tests/unit/test_cart.py::test_cart_option_invalid_reask_literal_matches_issue_570 \
  tests/unit/test_cart.py::test_cart_option_hint_fallback_literal_matches_issue_570 \
  tests/unit/test_cart.py::test_cart_option_reask_strips_seller_text \
  tests/unit/test_cart.py::test_cart_option_hint_fallback_without_total_has_no_extra_line \
  tests/unit/test_cart.py::test_cart_option_reask_reproduces_issue_570_symptom \
  tests/unit/test_cart.py::test_cart_add_reask_surcharge_option_on_own_line \
  tests/unit/test_cart.py::test_options_text_empty_list_falls_back_to_default_label \
  tests/unit/test_cart.py::test_cart_option_numeric_prefix_name_not_escaped -q
```

Expected: collection succeeds and all 13 selected tests execute. The result is `12 failed, 1 passed`: `test_numbered_option_rows_bolds_complete_labels_in_order` fails at runtime with `AttributeError: module 'app.agents.buyer.cart.graph' has no attribute '_numbered_option_rows'`; the sanitizer test, all five literal output tests, and the adjacent formatting-preservation tests fail with old unnumbered/plain output versus the new `N. **label**` literal expectations; only `test_options_text_empty_list_falls_back_to_default_label` passes because `옵션` degradation is intentionally unchanged. This is the required RED observation. Stop and investigate if collection fails, the failure shape differs, or the command passes; do not add a production stub or edit `graph.py` until these expected failures have been observed.

- [ ] **Step 6: Add the presentation-only helper beside `_option_label`**

Insert after `_has_surcharge` and before `_options_text`:

```python
def _numbered_option_rows(labels: Sequence[str]) -> str:
    """완성된 옵션 표시 라벨을 1-based 번호 목록과 굵은 글씨로 꾸민다(이슈 #582)."""
    return "\n".join(f"{index}. **{label}**" for index, label in enumerate(labels, start=1))
```

This helper must remain presentation-only: do not call `_strip_unsafe`, inspect `CartOption`, reconstruct surcharge text, escape Markdown, filter labels, parse shopper input, or derive `optionId` inside it.

- [ ] **Step 7: Route non-empty `_option_label` results through the helper without changing fallback behavior**

Replace the final two lines of `_options_text` with:

```python
    labels = [label for opt in options if (label := _option_label(opt))]
    return _numbered_option_rows(labels) if labels else "옵션"
```

Keep `_options_text([]) == "옵션"`; `옵션` is degradation copy, not an actual selectable row.

- [ ] **Step 8: Route sanitized hint names through the same helper and leave the summary outside it**

In `_cart_option_required_text`, replace only the `if names:` body at the empty-I-2 hint fallback with:

```python
            if names:
                lines = ["옵션을 선택해 주세요:", _numbered_option_rows(names)]
                if hint.total is not None and hint.total > len(names):
                    lines.append(f"외 {hint.total - len(names)}개")
                lines.append("어떤 걸로 담을까요?")
                return "\n".join(lines)
```

Do not move `_strip_unsafe`; `names` must still be sanitized and empty-filtered before numbering. Do not pass `외 N개` to `_numbered_option_rows`.

- [ ] **Step 9: Update only directly stale formatter comments/docstrings**

Adjust `_options_text`, `_options_prompt`, and the hint-fallback comment to say that #570 established one-option-per-line and #582 adds one-based ordered rows with a complete bold label. Do not rewrite branch semantics or unrelated historical comments.

- [ ] **Step 10: Run the same focused tests and verify GREEN**

Run the exact command from Task 1 Step 5.

Expected: `13 passed`; every affected literal has unchanged lead/tail boundaries, the surcharge closes before `**`, sanitization precedes contiguous numbering, `외 2개` is plain, the raw `4. ` name remains unescaped inside the bold span, and `_options_text([])` remains exactly `옵션`.

- [ ] **Step 11: Run the complete cart unit suite**

Run:

```bash
uv run pytest tests/unit/test_cart.py -q
```

Expected: PASS with no failures. Any failure in pending-option parsing, autoselection, color narrowing, sold-out degradation, or state handling indicates scope leakage; fix the shared presentation wiring rather than changing those behaviors.

- [ ] **Step 12: Commit the tested implementation with Lore trailers**

Run:

```bash
git add app/agents/buyer/cart/graph.py tests/unit/test_cart.py
git commit \
  -m "Make buyer option choices scannable in rendered chat" \
  -m "Constraint: Limit presentation changes to five #572 buyer cart re-ask outputs" \
  -m "Rejected: Decorating five call sites independently | duplicates Markdown construction and risks drift" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Directive: Keep sanitization and complete-label construction ahead of row decoration" \
  -m "Tested: uv run pytest tests/unit/test_cart.py -q" \
  -m "Not-tested: frontend rendering and seller flows are out of scope"
```

Expected: one commit containing only `app/agents/buyer/cart/graph.py` and `tests/unit/test_cart.py`.

---

### Task 2: Synchronize the API rendering contract and changelog

**Files:**
- Modify: `docs/api-spec.md:618-650,769,1931,2936`
- Modify: `CHANGELOG.md:14-32`

**Interfaces:**
- Consumes: implemented `_numbered_option_rows(labels: Sequence[str]) -> str` behavior and the five passing literal regressions.
- Produces: API-spec history entry and Unreleased changelog record for issue #582; no wire schema, event, field, or error-code change.

- [ ] **Step 1: Update the buyer `token.text` rendering facts in `docs/api-spec.md`**

In §3.1 `(2) token`, replace the stale sentence saying `**강조**` is unused with this exact statement:

```markdown
- 지금 실제로 내보내는 곳(현행 동작 명시) — 옵션 되물음(§4.1 `CART_OPTION_REQUIRED`/
  `CART_OPTION_INVALID`)·장바구니 조회·찜 목록·주문 상태(§4.10)가 `\n` 나열을 쓰고, 주문 상태는
  `1. `·`- `도 쓴다. **[#582] 구매자 장바구니 옵션 되물음은 실제 선택지마다 `N. **기존 표시
  라벨**`을 쓰며, 추가금 접미사도 같은 굵은 범위 안에 둔다.**
```

In the §3.1 option-reask paragraph and §4.1 `CART_OPTION_REQUIRED` AI behavior cell, add one sentence stating that the five buyer cart re-ask outputs render each actual option as `N. **existing label**`, while lead/tail lines, sanitization, empty degradation, raw-name semantics, and hint `외 N개` placement remain unchanged. Do not describe this as a new SSE event or Spring wire contract.

- [ ] **Step 2: Add the API-spec history row**

Add this new row immediately above v0.32.17:

```markdown
| v0.32.18 | 2026-08-11 | **[#582] 구매자 장바구니 옵션 되물음의 실제 선택지 행을 `N. **기존 표시 라벨**` 형식으로 바꿨다.** #455 조건 좁힘·#454 색상 미충족·기본 `CART_OPTION_REQUIRED`·`CART_OPTION_INVALID`·I-1 힌트 폴백의 다섯 출력만 대상이며, 추가금 접미사는 굵은 범위 안에 유지한다. 안내/마무리 줄, 정제, 원시 이름 의미, 빈 목록 강등, `외 N개` 배치는 불변이다. **와이어 계약(엔드포인트·SSE 이벤트·필드·오류 코드) 불변이며 FE·판매자·입력 해석은 범위 밖이다.** |
```

- [ ] **Step 3: Add one `Changed` entry to `CHANGELOG.md`**

Insert at the top of `## [Unreleased]` → `### Changed`:

```markdown
- **#582 — 구매자 장바구니 옵션 되물음의 실제 선택지를 번호 목록 + 굵은 라벨로 표시한다**
  (api-spec §3.1·§4.1, v0.32.18). #455 조건 좁힘·#454 색상 미충족·기본
  `CART_OPTION_REQUIRED`·`CART_OPTION_INVALID`·I-1 힌트 폴백의 다섯 출력에서 각 선택지가
  `N. **기존 표시 라벨**`로 나오며, 추가금 접미사도 굵은 범위 안에 남는다. 안내/마무리 줄,
  기존 정제와 원시 이름 의미, 빈 목록 강등, 힌트의 `외 N개` 배치는 바꾸지 않았다. 입력 해석,
  `screen.columns`, 추천 카드, FE, 판매자, HTML·링크·그 밖의 마크다운은 범위 밖이다.
```

- [ ] **Step 4: Verify documentation scope and stale claims**

Run:

```bash
rg -n "#582|N\. \*\*|강조.*현재 어디서도" docs/api-spec.md CHANGELOG.md
git diff --check
```

Expected: #582 appears in the current behavior text, one API history row, and one Unreleased changelog entry; `강조**는 현재 어디서도 내보내지 않는다` returns no match; `git diff --check` exits 0.

- [ ] **Step 5: Commit the synchronized docs with Lore trailers**

Run:

```bash
git add docs/api-spec.md CHANGELOG.md
git commit \
  -m "Keep the option rendering contract aligned with buyer output" \
  -m "Constraint: Document an AI presentation change without changing the SSE or Spring wire schema" \
  -m "Rejected: Describing input parsing or frontend behavior | both are outside #582" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Directive: Preserve the five-path boundary and plain hint summary in future edits" \
  -m "Tested: rg #582 and Markdown contract anchors; git diff --check" \
  -m "Not-tested: external documentation mirrors"
```

Expected: one documentation commit containing only `docs/api-spec.md` and `CHANGELOG.md`.

---

### Task 3: Prove repository-wide completion and scope

**Files:**
- Verify: `app/agents/buyer/cart/graph.py`
- Verify: `tests/unit/test_cart.py`
- Verify: `docs/api-spec.md`
- Verify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the implementation and documentation commits from Tasks 1 and 2.
- Produces: fresh completion evidence for the exact five-path change and its excluded surfaces.

- [ ] **Step 1: Run formatting and static lint checks on the touched Python files**

Run:

```bash
uv run ruff format --check app/agents/buyer/cart/graph.py tests/unit/test_cart.py
uv run ruff check app/agents/buyer/cart/graph.py tests/unit/test_cart.py
```

Expected: both commands exit 0 with no formatting or lint errors.

- [ ] **Step 2: Run the complete backend test suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS with zero failures. If environment-only integration prerequisites prevent completion, record the exact failing command/output and still require the focused cart suite from Task 1 to pass; do not claim the full suite passed.

- [ ] **Step 3: Inspect the final diff for the five-path and exclusion boundaries**

Run:

```bash
git show --stat --oneline HEAD~1..HEAD
git diff HEAD~2..HEAD -- \
  app/agents/buyer/cart/graph.py \
  tests/unit/test_cart.py \
  docs/api-spec.md \
  CHANGELOG.md
git status --short
```

Expected: production changes are limited to `_numbered_option_rows`, `_options_text`, `_options_prompt` documentation, and the sanitized hint-name fallback; tests cover exactly the five literal outputs plus preservation regressions; docs mention no new wire surface; the worktree is clean. Confirm no frontend, seller, recommendation-card, `screen.columns`, input parsing, HTML/link, dependency, or unrelated file appears.

- [ ] **Step 4: Verify Lore commit shape and final stop condition**

Run:

```bash
git log -2 --format=full
```

Expected: both commits have an intent-first subject and the `Constraint`, `Rejected`, `Confidence`, `Scope-risk`, `Directive`, `Tested`, and `Not-tested` trailers. Stop only when all five literal outputs match `N. **existing label**`, preserved-behavior regressions and backend checks pass, documentation is synchronized, `git diff --check` is clean, and no excluded surface changed.
