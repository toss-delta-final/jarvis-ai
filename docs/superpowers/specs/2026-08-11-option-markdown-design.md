# Buyer Cart Option Re-ask Markdown Design

## Summary

Issue #582 changes only the presentation of selectable option rows in the five buyer cart option re-ask paths introduced by #572. Each real option becomes an ordered Markdown list item whose complete display label is bold:

```text
옵션을 선택해 주세요:
1. **블랙 / M**
2. **화이트 / L(+1,000원)**
어떤 걸로 담을까요?
```

The existing lead lines remain before the option rows and the existing tail lines remain after them. Option-label construction, sanitization, raw option-name semantics, extra-price wording, and empty-list degradation do not change.

## Goals

- Render every actual option row in all five buyer cart option re-ask paths as `<one-based index>. **<existing display label>**`.
- Keep additional-price text inside the bold label without changing its existing spelling or spacing, for example `2. **화이트 / L(+1,000원)**`.
- Make the change at the smallest shared formatting boundary used by those five paths.
- Preserve all behavior established by #572 except the option-row Markdown decoration.

## Affected Paths

The later implementation must cover exactly the five buyer-facing cart output paths changed by #572 in `app/agents/buyer/cart/graph.py`:

1. the #455 condition-narrowed `CART_OPTION_REQUIRED` prompt;
2. the #454 unmet-color-condition `CART_OPTION_REQUIRED` prompt;
3. the default `CART_OPTION_REQUIRED` prompt;
4. the `CART_OPTION_INVALID` retry prompt; and
5. the empty-I-2-list fallback that displays sanitized I-1 hint names.

In the fifth path, each sanitized hint name is an actual option row and receives numbering and bold markup. The `외 N개` summary is not an option row, so it remains an unnumbered, non-bold line after the actual option rows. No recommendation-card, seller, or frontend path is included.

Each path keeps its current path-specific lead and tail content. Only the contiguous rows representing selectable options change shape.

## Output Contract

For each sanitized, displayable option at zero-based position `i`, emit one line:

```text
${i + 1}. **${existingOptionLabel}**
```

The formatter must reuse the existing option-label result without re-parsing or reconstructing it. Consequently:

- numbering is one-based and follows the existing option order;
- the full label, including any existing additional-price suffix, is inside one pair of `**` delimiters, for example `2. **레드(+1,000원)**`;
- lead lines, option lines, and tail lines retain the current newline boundaries and ordering;
- option names retain their existing raw-name semantics after the current sanitization step;
- no links, HTML, nested Markdown, or additional Markdown forms are introduced.

## Formatter Boundary and Data Flow

Introduce one presentation-only helper, tentatively `_numbered_option_rows`, at the label boundary in `app/agents/buyer/cart/graph.py`: it accepts a sequence of already-sanitized, non-empty display-label strings and joins them as one-based ordered-list rows with each complete label bolded. This is smaller than moving whole-message assembly and broad enough to serve both input shapes: `_option_label(CartOption)` results and sanitized `OptionHint.names` strings.

1. Each buyer cart path reaches its existing re-ask branch.
2. Existing code obtains options and applies the existing sanitization/filtering behavior.
3. For I-2 `CartOption` rows, existing `_option_label` construction produces the complete label, including additional price when applicable. For the hint fallback, the existing `_strip_unsafe` result is the complete label.
4. The shared label-row formatter adds the one-based ordered-list prefix and wraps each complete label in bold delimiters.
5. Existing message assembly places the formatted rows between unchanged lead and tail sections.

The formatter owns presentation only. It must not interpret user input, select an option, derive an `optionId`, change option ordering, or modify message routing.

### Alternatives considered

- **Recommended: shared formatter over completed labels.** It covers all five paths without coupling the hint fallback to `CartOption` or changing whole-message assembly.
- **Change `_options_text` only.** This is too narrow because the hint fallback operates on strings and would retain a sixth, manual rendering shape or duplicate Markdown construction.
- **Generalize `_options_prompt` to own all rows and summaries.** This is broader than required and risks changing the fifth path's `외 N개` placement and the invalid path's intentionally absent tail.

## Empty and Error Behavior

- If `_options_text` receives no displayable `CartOption` labels, preserve its current fallback text `옵션` exactly; do not turn that fallback word into a numbered/bold actual-option row.
- If `CART_OPTION_REQUIRED` supplies no I-2 options, preserve the current branch behavior: use sanitized I-1 hint names when present, otherwise emit the existing sold-out degradation sentence. If sanitization removes every hint name, follow that same sold-out degradation path.
- If an option is removed or normalized by current sanitization, preserve that outcome before Markdown formatting.
- Preserve current handling of missing, malformed, or unusual option fields. This issue does not add fallback labels or validation policy.
- Preserve current newline behavior when lead or tail sections vary by path.
- Do not escape or reinterpret raw option-name content beyond existing sanitization. The later implementation must apply Markdown delimiters to the existing display label, not introduce a new sanitization contract.

## Acceptance Criteria

1. Each of the five #572 buyer cart option re-ask paths renders every actual option row as an ordered Markdown item with the complete label bolded.
2. A normal option renders exactly as `1. **블랙 / M**` for the corresponding existing label.
3. An option with an additional price keeps the existing suffix unchanged and within the bold span, such as `2. **레드(+1,000원)**`.
4. Numbering starts at 1, remains contiguous, and preserves the existing option order after sanitization.
5. Each path's existing lead lines, tail lines, and newline structure remain unchanged outside the option rows.
6. Existing option sanitization, raw option-name semantics, additional-price wording, and empty-list degradation remain unchanged.
7. All five paths use the smallest practical shared option-row formatter; the implementation does not duplicate Markdown construction across call sites.
8. In the hint fallback, actual names are numbered and bolded while `외 N개` remains an unnumbered, non-bold summary line.
9. No behavior or artifact listed as out of scope changes.

## Later Implementation Surface

After this design is approved, implementation should be limited to:

- `app/agents/buyer/cart/graph.py`, limited to the shared completed-label row formatter and minimal wiring through `_options_text`, `_options_prompt`, and the hint-name fallback;
- the five call sites only if a minimal wiring adjustment is required to route them through that formatter;
- literal regression expectations for those five outputs;
- the API specification and CHANGELOG entries required by the repository's implementation policy.

Production code, tests, API specification, and CHANGELOG are intentionally unchanged in this design phase.

## Later Test Plan

- Add or update a focused formatter test proving exact ordered-list and bold syntax for a normal label.
- Prove the additional-price suffix remains inside the closing `**`.
- Update the five literal #572 regression assertions in `tests/unit/test_cart.py`, one for each affected path, covering unchanged lead/tail lines and changed option rows.
- Preserve or add coverage for sanitization before numbering, including contiguous one-based indices after filtered entries.
- Preserve the `_options_text([]) == "옵션"` regression, the empty-I-2/no-usable-hint sold-out degradation, and the hint `외 N개` placement; assert that none emits fabricated option rows or Markdown debris.
- Cover unusual option-name content according to the existing raw-name and sanitization contract, without adding Markdown escaping behavior.
- Run the relevant backend test suite, then repository lint/typecheck/static checks required for the touched implementation surface.

## Risks and Mitigations

- **Formatting at five call sites can drift.** Centralize only the row decoration at the smallest shared boundary and assert every path's literal output.
- **Bold delimiters can exclude the price suffix.** Format the already-complete label, then wrap it once; test the exact closing-delimiter position.
- **A broader refactor can alter #572 behavior.** Leave data retrieval, sanitization, label construction, message assembly, and empty-state logic in place.
- **Markdown-sensitive raw names can produce surprising rendering.** Preserve current raw-name semantics deliberately; escaping policy is a separate concern.
- **Empty collections can create invalid list fragments.** Invoke row decoration only for existing displayable rows and preserve the established degradation path.
- **The hint summary can be mistaken for a selectable option.** Number and bold only sanitized names; keep `외 N개` outside the shared formatter.

## Out of Scope

- Parsing shopper replies or mapping text such as `2번` to an `optionId`.
- Recommendation-screen `columns`, coordinates, row/column resolution, or recommendation-card behavior.
- Frontend changes of any kind.
- Seller flows or seller-facing messages.
- HTML, links, or Markdown beyond the ordered-list prefix and bold label required here.
- New option sanitization, escaping, fallback naming, sorting, pricing, or validation semantics.
- Production code, test code, API specification, or CHANGELOG edits during this design-only issue phase.

## Implementation Stop Condition

The later implementation is complete only when all five buyer cart re-ask outputs satisfy the exact row contract, preserved behaviors have regression evidence, required backend checks pass, and the implementation diff contains no excluded surface. This document's phase stops earlier: after this single design file is self-reviewed, verified as the only tracked change, and committed.

## Self-Review

- **Placeholders:** No placeholder markers, unresolved choices, or incomplete requirements remain.
- **Consistency:** The output examples, formatter contract, acceptance criteria, implementation surface, and test plan all require the same one-based ordered-list row with one bold span around the existing complete label.
- **Scope:** The design is restricted to the five buyer cart option re-ask paths from #572 and one shared presentation boundary.
- **Ambiguity:** “Bold label” explicitly includes the additional-price suffix; sanitization occurs before numbering; empty-list degradation and raw option-name semantics remain unchanged.
- **Phase boundary:** This commit contains design documentation only. Implementation requires a later approved phase.
