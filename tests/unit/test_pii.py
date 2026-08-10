"""app/core/pii.py — 하드 PII 결정론적 탐지·치환 (이슈 #321).

합성 값만 쓴다: 전화번호는 `0000` 계열, 주민번호는 체크섬이 틀린 값, 카드는 공개 테스트 BIN
(4111 1111 1111 1111 계열), 이메일은 RFC 2606 예약 도메인(`example.com`). 실존 가능한 값은
어설션 메시지에도 싣지 않는다.
"""

from __future__ import annotations

import random

import pytest

from app.core.config import get_settings
from app.core.pii import PiiClass, contains_hard_pii, detect_hard_pii, redact


# ─────────── 클래스별 탐지 ───────────


def test_email_detected() -> None:
    assert contains_hard_pii("연락처는 tester@example.com 입니다") is True
    assert detect_hard_pii("tester@example.com") == frozenset({PiiClass.EMAIL})


def test_kr_mobile_detected_with_and_without_separators() -> None:
    assert contains_hard_pii("010-0000-0000 으로 연락주세요") is True
    assert contains_hard_pii("01000000000 으로 연락주세요") is True
    assert detect_hard_pii("010 0000 0000") == frozenset({PiiClass.KR_MOBILE})


def test_kr_rrn_detected_by_shape_even_with_bad_checksum() -> None:
    # 체크섬은 자문용이라 모양(+ 월/일 유효성)만 맞으면 차단한다.
    assert contains_hard_pii("주민번호 990101-1234567 인데") is True
    assert detect_hard_pii("990101-1234567") == frozenset({PiiClass.KR_RRN})


def test_kr_rrn_foreign_registration_gender_digits_5_to_8() -> None:
    assert contains_hard_pii("990101-5234567") is True
    assert contains_hard_pii("990101-8234567") is True


def test_kr_rrn_rejects_invalid_month_or_day() -> None:
    assert contains_hard_pii("991301-1234567") is False  # 13월
    assert contains_hard_pii("990132-1234567") is False  # 32일
    assert contains_hard_pii("990100-1234567") is False  # 0일


def test_kr_rrn_gender_digit_out_of_range_not_detected() -> None:
    assert contains_hard_pii("990101-0234567") is False  # 0은 성별자리 밖
    assert contains_hard_pii("990101-9234567") is False  # 9는 성별자리 밖


def test_card_pan_requires_luhn_and_iin_prefix() -> None:
    # 공개 테스트 BIN(Luhn 통과) — 실카드 아님.
    assert contains_hard_pii("카드번호 4111 1111 1111 1111 입니다") is True
    assert detect_hard_pii("4111111111111111") == frozenset({PiiClass.CARD_PAN})


def test_card_pan_rejects_bigint_id_that_fails_luhn_or_iin() -> None:
    # 이 리포의 상품/주문 id는 BIGINT라 16자리 런과 충돌할 수 있다 — Luhn+IIN 둘 다 없으면 통과.
    assert contains_hard_pii("주문번호 1234567890123456 확인해줘") is False


def test_kr_bank_account_requires_co_occurrence_anchor() -> None:
    assert contains_hard_pii("국민은행 계좌 123456789012 로 입금해줘") is True
    # 앵커 없는 순수 자릿수 런은 계좌로 보지 않는다(체크섬 없는 자릿수만으론 오탐 기계).
    assert contains_hard_pii("주문번호 123456789012 확인해줘") is False


def test_secret_token_bearer_and_prefixed_keys() -> None:
    assert contains_hard_pii("Authorization: Bearer abcdef123456") is True
    assert contains_hard_pii("키는 sk-abcdefghijklmnop 입니다") is True
    assert contains_hard_pii("키는 lsv2_abcdefghijklmnop 입니다") is True


# ─────────── redact — 부분 치환 금지, 닫힌 어휘 placeholder ───────────


def test_redact_replaces_full_span_with_closed_vocabulary_placeholder() -> None:
    text, classes = redact("제 번호는 010-0000-0000 이고 tester@example.com 로도 연락돼요")
    assert "[전화번호]" in text
    assert "[이메일]" in text
    assert "0000" not in text
    assert "example.com" not in text
    assert classes == frozenset({PiiClass.KR_MOBILE, PiiClass.EMAIL})


def test_redact_no_op_when_no_pii() -> None:
    text, classes = redact("30000-50000원 사이 무선 이어폰 추천해줘")
    assert text == "30000-50000원 사이 무선 이어폰 추천해줘"
    assert classes == frozenset()


def test_redact_handles_none_and_empty() -> None:
    assert redact(None) == ("", frozenset())
    assert redact("") == ("", frozenset())


def test_contains_and_detect_handle_none() -> None:
    assert contains_hard_pii(None) is False
    assert detect_hard_pii(None) == frozenset()


# ─────────── zero-width 우회 방어 (skeleton 위에서 탐지) ───────────


def test_zero_width_characters_between_digits_do_not_evade_detection() -> None:
    zwsp = "​"
    obfuscated = zwsp.join("01000000000")
    assert contains_hard_pii(obfuscated) is True
    text, classes = redact(obfuscated)
    assert classes == frozenset({PiiClass.KR_MOBILE})
    assert "[전화번호]" in text


# ─────────── 정상 취향 오탐 방지 — 회귀 표본 + 생성 corpus (#208 lesson 5) ───────────


_BENIGN_UTTERANCES = [
    "30000-50000원 사이로 보여줘",
    "평점은 4-5 사이가 좋아요",
    "3-5만원대 무선이어폰 찾아줘",
    "가격대는 10000-20000",
    "평점 3.5 이상만",
    "주문번호 1234567890123456 확인 부탁해요",
    "상품 모델명 XR-2024-PRO 알려줘",
    "재고 12개 남았대요",
    "5000원 할인 쿠폰 있나요",
    "옵션 3번으로 담아줘",
    "2개 더 살까 고민 중이에요",
    "3일 안에 배송 오나요",
    "10만원 이하로 골라줘",
    "사이즈는 M 사이즈로",
    "색상은 검정으로 부탁해요",
]


def test_benign_price_and_rating_ranges_pass_through() -> None:
    """고정 15개 회귀 표본 — 특정 문장이 다시 걸리면 즉시 원인을 짚을 수 있게 남겨 둔다.

    [F3, #321 리뷰 2라운드] 실제 오탐**률** 측정은 corpus 가 필요하다(아래
    `test_order_number_like_digit_runs_false_positive_rate_is_bounded`) — 이 15개는 그
    표본으로 대체되지 않는, 알려진 특정 문장에 대한 회귀 방지용이다.
    """
    false_positives = [text for text in _BENIGN_UTTERANCES if contains_hard_pii(text)]
    assert false_positives == []


def _random_digit_run(rng: random.Random, length: int) -> str:
    """BIGINT 표기 자릿수 런 합성 — 실제 id 처럼 앞자리는 0이 아니다."""
    first = rng.choice("123456789")
    rest = "".join(rng.choice("0123456789") for _ in range(length - 1))
    return first + rest


def test_order_number_like_digit_runs_false_positive_rate_is_bounded() -> None:
    """[F3, #321 리뷰 2라운드] 진짜 생성 corpus.

    이전 버전은 같은 15개 상수를 `rng.sample`로 순서만 섞어 시드가 아무 일도 하지 않았다
    (`#208 lesson 5`가 경고한 바로 그 형태). 여기는 매 표본마다 `random.Random(321)`로 새
    자릿수 런(6~19자리, 주문번호 모양)을 생성해 실제 오탐**률**을 잰다.

    실측(2026-08-10, 이 시드·생성기·표본 크기 2,000 기준): **1.40%**(28/2000, CARD_PAN 다수 +
    KR_RRN 일부 — Luhn 통과 1/10 × IIN 접두 매칭 ~1/4 인 구조적 기댓값과 일치). 탐지기는
    그대로 둔다(정밀도를 올리려 recall 을 깎지 않는다) — 미탐(카드번호가 pg-profile → Google
    임베딩 → 프롬프트 → 트레이스로 퍼지는 비가역 손실) 비용이 오탐(그 턴의 즉시 저장 1건,
    발화는 치환된 채 버퍼에 남아 델타 경로가 회수하는 유계 손실)보다 크다는 판단이다
    (`app/core/pii.py` 모듈 docstring 참고). 상한(3%)은 실측치보다 넉넉히 잡아, 누가 탐지기를
    넓혀 오탐이 폭증하면 이 단언이 시끄럽게 깨지게 한다.
    """
    rng = random.Random(321)
    corpus = [
        f"주문번호 {_random_digit_run(rng, rng.randint(6, 19))} 확인해줘" for _ in range(2000)
    ]
    false_positives = [text for text in corpus if contains_hard_pii(text)]
    fp_rate = len(false_positives) / len(corpus)
    assert fp_rate < 0.03, f"오탐률 {fp_rate:.3%} 가 상한(3%)을 넘었다 — 탐지기 변경을 의심할 것"


# ─────────── config 튜너블 ───────────


def test_bank_account_anchor_window_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "pii_bank_account_anchor_window", 0)
    # 창을 0으로 좁히면 앵커 단어가 자릿수 런에서 너무 멀어져(20자 이상) 더 이상 안 걸린다.
    text = "국민은행에 문의하려던 참인데 아무튼 제 번호는 123456789012 입니다"
    assert contains_hard_pii(text) is False
