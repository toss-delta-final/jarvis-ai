"""하드 PII 결정론적 탐지·치환 — 단일 정본 (이슈 #321).

순수·동기·무 I/O. `record_remember`(app/agents/profile/builder.py) 등 hot-path 에서 await 되는
경로 앞에 놓이므로 **어떤 경우에도 예외를 던지지 않는다** — 예외 하나가 턴을 500 으로 만든다
(공개 함수는 전부 자체 방어하고, 실패 시 fail-closed 로 물러난다).

탐지는 `app/core/text.py::_security_skeleton` 위에서 한다 — 제어문자·variation selector·tag
문자를 걷어낸 뒤 매칭해야 폭 0 문자를 숫자 사이에 끼워 넣는 우회가 안 통한다
(`app/agents/seller/middleware.py::mask_output` 와 같은 방식).

**순환 임포트 주의**: 이 모듈은 `config`·`text` 에만 의존한다. `app/core/tracing.py` 를
임포트하지 않는다 — 대신 tracing.py 가 이미 소유한 카나리아 리터럴(이메일·전화·시크릿 토큰)과
동일한 정규식을 이 모듈이 **독립적으로** 소유한다. tracing.py 의 기존 검증기·면제 목록은
건드리지 않는다.

**[F4, #321 리뷰 2라운드] 알려진 한계 — 오탐, 수치로 남긴다.** 이 리포의 교훈("설계 문서에
'알려진 편향/한계'를 적었다면 그 크기를 수치로 남긴다 — 적기만 하면 다음 사람이 '수용
가능'으로 읽고 넘어간다")에 따라 실측치를 적는다(`tests/unit/test_pii.py::
test_order_number_like_digit_runs_false_positive_rate_is_bounded`, `random.Random(321)`,
표본 2,000):

| 입력 클래스 | 오탐률 | 비고 |
|---|---|---|
| BIGINT 모양 id 를 담은 문장(`"주문번호 {6~19자리} 확인해줘"`) | **1.40%** (28/2000) | CARD_PAN 다수 + KR_RRN 일부 |
| 맨 16자리 런 | **2.375%** (475/20000) | Luhn 1/10 × IIN 접두 ~1/4 ≈ 구조적 기댓값 |
| 무작위 32-hex | **0.055%** (11/20000) | #208 이 문제 삼았던 클래스 |

오탐은 recall 을 위해 **의도적으로 감수한다** — 미탐(카드번호가 pg-profile → Google 임베딩 →
프롬프트로 퍼짐)은 비가역이고, 오탐(그 턴의 즉시 저장 1건)은 버퍼 경로(치환된 채 남아 델타
추출이 회수)가 유계로 흡수한다.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.core.config import get_settings
from app.core.text import _security_skeleton


class PiiClass(StrEnum):
    """하드 PII 탐지 클래스 — 로그에 클래스 자체를 남기지 마라(REQ-PGRAPH-075/076)."""

    EMAIL = "email"
    KR_MOBILE = "kr_mobile"
    KR_RRN = "kr_rrn"
    CARD_PAN = "card_pan"
    KR_BANK_ACCOUNT = "kr_bank_account"
    SECRET_TOKEN = "secret_token"


_PLACEHOLDERS: dict[PiiClass, str] = {
    PiiClass.EMAIL: "[이메일]",
    PiiClass.KR_MOBILE: "[전화번호]",
    PiiClass.KR_RRN: "[주민등록번호]",
    PiiClass.CARD_PAN: "[카드번호]",
    PiiClass.KR_BANK_ACCOUNT: "[계좌번호]",
    PiiClass.SECRET_TOKEN: "[비밀토큰]",
}

# app/core/tracing.py::_TEXT_CANARY_PATTERNS[2] 와 같은 리터럴 — 독립 소유(모듈 docstring 참조).
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
# app/core/tracing.py::_NUMERIC_CANARY_PATTERNS[0] 과 같은 리터럴.
_KR_MOBILE_RE = re.compile(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)")
# app/core/tracing.py::_TEXT_CANARY_PATTERNS[0]/[1] 과 같은 리터럴.
_BEARER_RE = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(r"\b(?:sk-|lsv2_)[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)

# YYMMDD + 성별자리[1-8](외국인등록번호 5~8 포함) + 6자리. 월·일 유효성은 별도 검사한다.
# 체크섬은 자문용이라 여기서 계산하지 않는다 — 모양(+ 날짜 유효성)이 맞으면 체크섬 실패여도
# 차단한다(13자리 주민번호 모양은 커머스 텍스트에 사실상 없다는 판단, 이슈 #321 결정 A).
_KR_RRN_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[ -]?([1-8])(\d{6})(?!\d)")
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

# 카드 13~19자리 / 계좌 10~14자리 — 자릿수 총합 기준(구분자 자체는 세지 않는다).
_CARD_RUN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_BANK_ACCOUNT_RUN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){9,13}\d(?!\d)")

_IIN_PREFIXES = (
    re.compile(r"^4"),
    re.compile(r"^5[1-5]"),
    re.compile(r"^2[2-7]"),
    re.compile(r"^3[47]"),
    re.compile(r"^6011"),
    re.compile(r"^62"),
)

_BANK_ANCHOR_WORDS = (
    "계좌",
    "입금",
    "송금",
    "은행",
    "농협",
    "국민",
    "신한",
    "우리",
    "하나",
    "기업",
    "카카오뱅크",
    "토스뱅크",
    "새마을",
    "우체국",
)

_SEPARATOR_RE = re.compile(r"[ -]")


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = ord(char) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _has_iin_prefix(digits: str) -> bool:
    return any(pattern.match(digits) for pattern in _IIN_PREFIXES)


def _has_bank_anchor(body: str, start: int, end: int, window: int) -> bool:
    window = max(window, 0)
    context = body[max(0, start - window) : end + window]
    return any(word in context for word in _BANK_ANCHOR_WORDS)


def _scan(text: str | None) -> list[tuple[int, int, PiiClass]]:
    """원문 좌표(skeleton.source_span 변환 후)의 (start, end, class) 매치를 우선순위대로 낸다."""
    if not text:
        return []
    skeleton = _security_skeleton(text)
    body = skeleton.text
    settings = get_settings()
    claimed: list[tuple[int, int]] = []
    matches: list[tuple[int, int, PiiClass]] = []

    def _claim(start: int, end: int, cls: PiiClass) -> None:
        for claimed_start, claimed_end in claimed:
            if start < claimed_end and claimed_start < end:
                return  # 이미 다른 클래스가 확정한 구간과 겹침 — 먼저 온 매치가 우선한다
        claimed.append((start, end))
        source_start, source_end = skeleton.source_span(start, end)
        matches.append((source_start, source_end, cls))

    for match in _EMAIL_RE.finditer(body):
        _claim(match.start(), match.end(), PiiClass.EMAIL)

    for match in _KR_MOBILE_RE.finditer(body):
        _claim(match.start(), match.end(), PiiClass.KR_MOBILE)

    for match in _KR_RRN_RE.finditer(body):
        month = int(match.group(2))
        day = int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= _DAYS_IN_MONTH[month - 1]:
            _claim(match.start(), match.end(), PiiClass.KR_RRN)

    for match in _CARD_RUN_RE.finditer(body):
        digits = _SEPARATOR_RE.sub("", match.group(0))
        if 13 <= len(digits) <= 19 and _has_iin_prefix(digits) and _luhn_valid(digits):
            _claim(match.start(), match.end(), PiiClass.CARD_PAN)

    for match in _BANK_ACCOUNT_RUN_RE.finditer(body):
        digits = _SEPARATOR_RE.sub("", match.group(0))
        if 10 <= len(digits) <= 14 and _has_bank_anchor(
            body, match.start(), match.end(), settings.pii_bank_account_anchor_window
        ):
            _claim(match.start(), match.end(), PiiClass.KR_BANK_ACCOUNT)

    for match in _BEARER_RE.finditer(body):
        _claim(match.start(), match.end(), PiiClass.SECRET_TOKEN)
    for match in _SECRET_KEY_RE.finditer(body):
        _claim(match.start(), match.end(), PiiClass.SECRET_TOKEN)

    matches.sort(key=lambda item: item[0])
    return matches


def contains_hard_pii(text: str | None) -> bool:
    """저장 게이트가 쓰는 불리언 판정. 내부 오류는 fail-closed(True) — 탐지 실패가 저장 허용으로
    새면 안 된다(REQ-PGRAPH-071)."""
    try:
        return bool(_scan(text))
    except Exception:
        return True


def detect_hard_pii(text: str | None) -> frozenset[PiiClass]:
    """히트한 클래스 집합. 호출부는 로그에 이 값을 직접 싣지 마라(REQ-PGRAPH-075/076)."""
    try:
        return frozenset(cls for _, _, cls in _scan(text))
    except Exception:
        return frozenset()


def redact(text: str | None) -> tuple[str, frozenset[PiiClass]]:
    """매치 구간 전체를 닫힌 어휘 placeholder 로 치환한다(부분 치환 금지).

    내부 오류는 fail-closed — 원문 대신 빈 문자열을 돌려준다(버퍼·트레이스에 원문이 새는
    것보다 그 턴의 관측 하나를 잃는 편이 낫다).
    """
    if not text:
        return text or "", frozenset()
    try:
        matches = _scan(text)
    except Exception:
        return "", frozenset()
    if not matches:
        return text, frozenset()
    pieces: list[str] = []
    cursor = 0
    classes: set[PiiClass] = set()
    for start, end, cls in matches:
        pieces.append(text[cursor:start])
        pieces.append(_PLACEHOLDERS[cls])
        classes.add(cls)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), frozenset(classes)
