"""I-1 옵션 힌트(§4.6) 소비 — 순수 함수만 둔다(I/O 없음, 이슈 #455).

담기 권위는 끝까지 I-2(`CartOptionRequired.options`)다. 여기 함수는 그 400 목록을 사용자가
**이번 발화**·**누적 조건**으로 좁히는 어휘 매칭과, I-1 이 준 옵션 이름·개수를 되물음 문구·
정합 가드에 쓰기 위한 자료구조만 제공한다. `optionId` 를 이름으로 유추하지 않는다(#455 §2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re

from app.core.text import _strip_unsafe
from app.schemas.spring import CartOption, ProductSearchFilters

# 옵션명 세그먼트 분해 — "화이트/M", "레드, L" 처럼 판매자가 슬래시·쉼표·파이프·공백으로 조합값을
# 적는 관행(카탈로그 실측 52.8%, 패킷 §1)을 축 단위로 쪼갠다.
_SEGMENT_SPLIT_RE = re.compile(r"[/,|\s]+")

# 발화 토큰화 — 공백·문장부호 등 "단어가 아닌" 문자 전부를 구분자로 본다(#455 리뷰 F-1). 부분
# 문자열 포함(`seg in message`)은 "블루" ⊂ "블루투스"처럼 더 긴 낱말에 우연히 낀 세그먼트까지
# 매칭시킨다 — 토큰 단위로 끊어야 그 오탐을 막을 수 있다.
_TOKEN_SPLIT_RE = re.compile(r"[^\w]+")


@dataclass(frozen=True, slots=True)
class OptionHint:
    """I-1(§4.6)이 후보와 함께 준 옵션 메타 — 이름은 20개로 절단됐을 수 있고 `total` 은 그
    이름들의 원본 개수([#508] 품절 제외 후 "구매 가능한 것" 기준, api-spec §4.6)다."""

    names: tuple[str, ...]
    total: int | None


@dataclass(frozen=True, slots=True)
class OptionNarrowing:
    """400 옵션 목록을 발화·조건으로 좁힌 결과. 좁혀지지 않았으면(0건·전건) 빈 튜플.

    `condition_matched_all`(이슈 #454)은 `by_condition` 이 빈 튜플인 두 상황 —
    **0건 좁힘**과 **전건 일치**— 을 구별하기 위한 별도 신호다. `by_condition` 자체(기존 필드,
    의미 불변)만 보면 두 상황이 똑같이 빈 튜플이라, 전건이 조건에 맞는데 "조건에 맞는 게 없다"는
    취지의 문구(§2-B, `_cart_option_required_text`)를 낼 위험이 있다.
    """

    by_message: tuple[CartOption, ...]
    by_condition: tuple[CartOption, ...]
    condition_matched_all: bool


def condition_terms(*filters: ProductSearchFilters | None) -> tuple[str, ...]:
    """필터에서 사용자가 **명시**한 조건어만 뽑는다 — `color`·`attr_conditions` 값만.

    `keyword`·`semantic_query` 는 상품명/자연어라 옵션명 매칭엔 노이즈라 뺀다. 여러 필터를
    넘기면 먼저 넘긴 쪽의 어순·우선순위가 유지되며(호출부가 순서를 정한다), 공백만 다듬고
    중복은 제거한다.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for filters_ in filters:
        if filters_ is None:
            continue
        candidates: list[str] = []
        if filters_.color:
            candidates.append(filters_.color)
        if filters_.attr_conditions:
            candidates.extend(filters_.attr_conditions.values())
        for raw in candidates:
            term = raw.strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    return tuple(terms)


def _segments(name_norm: str) -> tuple[str, ...]:
    return tuple(seg for seg in _SEGMENT_SPLIT_RE.split(name_norm) if seg)


def _normalize(text: str) -> str:
    return text.strip().casefold()


def _tokenize(text_norm: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SPLIT_RE.split(text_norm) if t)


def _appears_as_token(word: str, tokens: Sequence[str], suffixes: Sequence[str]) -> bool:
    """`word` 가 발화 토큰으로 나타났는가 — 어떤 토큰이 `word + suffix` 와 정확히 같을 때만
    참이다(#455 리뷰 F-1). `word in message` 부분 문자열 검사와 달리 "블루" ⊂ "블루투스" 같은
    우연한 포함을 걸러낸다. `suffixes` 는 조사·꼬리말 허용목록(`""` 포함)이다."""
    return any(token == word + suffix for token in tokens for suffix in suffixes)


def _term_matches(term: str, name_norm: str, long_segments: Sequence[str]) -> bool:
    """R3 — 항목↔이름 매칭. `term`·`long_segments` 는 이미 `min_term_len` 이상으로 걸러진 값.

    이름(`name_norm`)과의 매칭이라 부분 문자열 포함을 그대로 쓴다 — `_appears_as_token` 의
    토큰 경계 규칙은 **발화** 매칭 전용이다(되물음 문구를 좁히는 R2/`by_condition` 에서만 쓰이고
    자동 선택 근거가 아니므로 오탐의 대가가 다르다, #455 리뷰 F-1)."""
    if term in long_segments:
        return True
    if term in name_norm:
        return True
    return any(seg in term for seg in long_segments)


def _color_equivalents(
    term: str, color_synonyms: Mapping[str, Sequence[str]] | None
) -> tuple[str, ...]:
    """조건어 하나를 색상 사전(승인 행만)의 등가 표기 집합으로 확장한다(이슈 #454).

    `term` 은 이미 `_normalize`(strip+casefold)를 거친 값이고, 사전 키도 같은 정규화 규약으로
    적재된다(`app.pipelines.color_synonyms.build_synonym_map`) — 그래서 여기서 다시 정규화하지
    않는다(두 번째 정규화 규약을 만들지 않는다, 패킷 §2-A-1). **사전 조회는 정확 일치뿐**이다
    (부분 문자열 조회 금지 — §1-3 의 SKU 코드 오탐을 사전 단계에서 막는다). 사전이 없거나
    미등록 표기면 원문 하나만 돌려준다 — `app.pipelines.color_synonyms.expand_color` 와 같은
    규약("미등록이면 원문 하나")이라 그 규약을 두 번 발명하지 않는다.
    """
    if color_synonyms is None:
        return (term,)
    equivalents = color_synonyms.get(term)
    if not equivalents:
        return (term,)
    return tuple(_normalize(value) for value in equivalents)


def narrow_options(
    options: Sequence[CartOption],
    *,
    message: str,
    terms: Sequence[str],
    min_term_len: int,
    match_suffixes: Sequence[str] = ("",),
    color_synonyms: Mapping[str, Sequence[str]] | None = None,
) -> OptionNarrowing:
    """400 옵션 목록을 이번 턴 발화(R1)·누적 조건(R1∪R2)으로 좁힌다.

    길이가 `min_term_len` 미만인 세그먼트·term 은 매칭에 쓰지 않는다("M" 단독이 아무 문장에나
    걸리는 것 방지). 매칭 집합이 `options` 전체와 같으면 좁힌 게 아니므로 빈 튜플로 돌려준다
    (0건도 빈 튜플) — 다만 두 상황(0건·전건)을 가르는 신호는
    `OptionNarrowing.condition_matched_all`(이슈 #454)로 별도로 준다.

    **발화 매칭(R1)은 토큰 경계 + 세그먼트 정확 일치만 본다**(#455 리뷰 F-1, F-5) — 두 갈래로만
    인정된다: (1) 옵션 세그먼트가 발화에 "나타났다" — 어떤 발화 토큰이 `그 세그먼트 +
    match_suffixes 의 원소`와 정확히 같을 때, (2) 발화에 토큰으로 나타난 term 이 옵션 세그먼트와
    **정확히 같을 때**(`term in long_segments`). 두 갈래 다 부분 문자열 포함을 쓰지 않는다 —
    "블루" ⊂ "블루투스", "그레이" ⊂ "그레이라이트"처럼 더 긴 낱말·이름에 우연히 낀 세그먼트까지
    매칭시키면 자동 선택(`by_message` 전용, `_select_auto_option`)이 사용자가 말한 적 없는
    옵션을 담아버린다. **`color_synonyms` 는 R1 에 절대 관여하지 않는다** — R1 은 자동 선택의
    유일 근거라 오탐이 곧 "사용자가 말한 적 없는 옵션 자동 담기"이므로(#455 리뷰 F-1 이 고정한
    비대칭, 이슈 #454 도 그대로 지킨다) 리터럴 일치만 본다.

    누적 조건 ↔ 이름 매칭(R2, `_term_matches`)은 되물음 문구를 좁히는 데만 쓰여 부분 문자열
    포함을 허용한다(오탐의 대가가 다르다). `color_synonyms`(이슈 #454, 승인된 색상 동의어 사전 —
    `app.pipelines.color_synonyms.get_synonym_map` 산출)를 주면 **R2 에만** 적용된다 — 조건어
    하나를 등가 표기 집합으로 확장해 그 표기 각각을 기존 `_term_matches` 규칙에 그대로 태운다
    (새 매칭 규칙을 만들지 않는다). 예: 조건어 "검정" + 옵션명 "블랙 / M" 은 사전이 없으면 R2가
    매칭하지 않지만, 사전이 검정↔블랙을 같은 묶음으로 담고 있으면 매칭한다. 확장으로 생긴
    표기에도 `min_term_len` 하한을 그대로 적용한다.
    """
    message_norm = _normalize(message)
    message_tokens = _tokenize(message_norm)
    long_terms = [term for raw in terms if len(term := _normalize(raw)) >= min_term_len]
    terms_in_message = [
        term for term in long_terms if _appears_as_token(term, message_tokens, match_suffixes)
    ]

    by_message: list[CartOption] = []
    by_condition: list[CartOption] = []
    for option in options:
        name_norm = _normalize(_strip_unsafe(option.name))
        segments = _segments(name_norm)
        long_segments = [seg for seg in segments if len(seg) >= min_term_len]

        r1 = any(
            _appears_as_token(seg, message_tokens, match_suffixes) for seg in long_segments
        ) or any(term in long_segments for term in terms_in_message)
        r2 = any(
            _term_matches(equivalent, name_norm, long_segments)
            for term in long_terms
            for equivalent in _color_equivalents(term, color_synonyms)
            if len(equivalent) >= min_term_len
        )

        if r1:
            by_message.append(option)
        if r1 or r2:
            by_condition.append(option)

    def _finalize(matched: list[CartOption]) -> tuple[CartOption, ...]:
        if len(matched) == len(options):
            return ()
        return tuple(matched)

    return OptionNarrowing(
        by_message=_finalize(by_message),
        by_condition=_finalize(by_condition),
        condition_matched_all=bool(options) and len(by_condition) == len(options),
    )


def color_condition_terms(
    terms: Sequence[str], color_synonyms: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """조건어 중 색상 사전(승인 행) 키와 **정확히 일치**하는 것만 원문 그대로 골라낸다(이슈 #454).

    "이 조건어가 색상어다"라는 판정은 정규화(strip+casefold) 후 사전 키 존재 여부로만 한다 —
    부분 일치를 쓰면 §1-3 이 보인 SKU 코드·사이즈 조각 오탐이 그대로 들어온다. 순서를 보존하고
    중복을 제거한다(원문 표기 기준 — `_options_text` 등 표시용 문구가 이 순서를 그대로 쓴다).
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in terms:
        term = raw.strip()
        if term and term not in seen and _normalize(term) in color_synonyms:
            seen.add(term)
            result.append(term)
    return tuple(result)


def options_have_color_axis(
    options: Sequence[CartOption], color_synonyms: Mapping[str, Sequence[str]]
) -> bool:
    """옵션명 세그먼트 중 하나 이상이 색상 사전(승인 행) 키와 **정확히 일치**하면 참(이슈 #454).

    "이 옵션 목록이 색상을 적고 있다"는 판정 근거 — 승인 사전 밖 표기(영문·약어·미승인 색명)는
    잡지 못하는 한계가 있다(패킷 §1-3, 그래서 고지 대상이 작다). 세그먼트 분해는
    `narrow_options` 와 같은 `_segments` 를 쓴다.
    """
    return any(
        seg in color_synonyms
        for option in options
        for seg in _segments(_normalize(_strip_unsafe(option.name)))
    )
