"""브랜드 추출 축 프로브의 채점·집계 결정론 테스트 (#466).

실 LLM 축은 수동 도구지만 **채점 함수는 CI 로 고정한다**(evals/README.md 규약3) — 지표
정의가 조용히 바뀌면 전/후 비교가 무의미해진다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.filter_axes.brand_probe import (
    CASES_PATH,
    aggregate,
    load_cases,
    run,
    score_negative,
    score_positive,
)


class _ScriptedLLM:
    """발화 → brand 값을 표로 돌려주는 최소 LLM(실 API 미호출)."""

    def __init__(self, table: dict[str, list[str] | None]) -> None:
        self._table = table

    async def complete(self, *, system: str, user: str, tier: str, max_tokens: int = 1024, **_):
        utterance = user.rsplit("USER_MESSAGE: ", 1)[1]
        brand = self._table.get(utterance)
        filters = {"brand": brand} if brand else {}
        return json.dumps(
            {"intent": "recommend", "reply": "", "semanticQuery": "q", "filters": filters},
            ensure_ascii=False,
        )

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


def test_cases_file_labels_are_self_consistent() -> None:
    """라벨한 브랜드가 발화에 실제로 있어야 verbatim 축의 기준이 성립한다."""
    cases = load_cases()
    assert cases["positives"] and cases["negatives"]
    assert len(cases["positives"]) == 20 and len(cases["negatives"]) == 4


def test_load_cases_rejects_label_not_present_in_utterance(tmp_path) -> None:
    bad = tmp_path / "brand_cases.json"
    bad.write_text(
        json.dumps(
            {
                "datasetVersion": "x",
                "positives": [{"caseId": "a", "brand": "삼성", "utterance": "LG 제품 아무거나"}],
                "negatives": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="발화에 없습니다"):
        load_cases(bad)


def test_load_cases_rejects_duplicate_case_ids(tmp_path) -> None:
    bad = tmp_path / "brand_cases.json"
    bad.write_text(
        json.dumps(
            {
                "datasetVersion": "x",
                "positives": [
                    {"caseId": "dup", "brand": "삼성", "utterance": "삼성 제품"},
                    {"caseId": "dup", "brand": "LG", "utterance": "LG 제품"},
                ],
                "negatives": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="중복"):
        load_cases(bad)


def test_score_positive_separates_extraction_from_surface_form() -> None:
    """번안값은 present 는 얻지만 verbatim·expected 는 잃는다 — 이 분리가 축의 존재 이유다."""
    assert score_positive(["애플"], "애플 제품 아무거나", "애플") == {
        "present": 1,
        "verbatim": 1,
        "expected": 1,
    }
    assert score_positive(["Apple"], "애플 제품 아무거나", "애플") == {
        "present": 1,
        "verbatim": 0,
        "expected": 0,
    }


def test_score_positive_catches_generic_noun_captured_as_brand() -> None:
    """ "제품"을 브랜드로 뽑으면 verbatim 은 통과해도 expected 가 잡는다."""
    assert score_positive(["제품"], "삼성 제품 아무거나", "삼성") == {
        "present": 1,
        "verbatim": 1,
        "expected": 0,
    }


def test_score_positive_requires_every_value_to_be_verbatim() -> None:
    """값 하나라도 발화 밖이면 verbatim 실패 — 한 개만 맞혀서 통과하지 않게."""
    assert score_positive(["삼성", "Samsung"], "삼성 제품 아무거나", "삼성")["verbatim"] == 0


def test_score_positive_treats_empty_and_blank_as_not_extracted() -> None:
    for value in (None, [], [""], ["   "]):
        assert score_positive(value, "삼성 제품 아무거나", "삼성")["present"] == 0


def test_score_negative_flags_any_spurious_brand() -> None:
    assert score_negative(["삼성"])["spurious"] == 1
    assert score_negative(None)["spurious"] == 0
    assert score_negative(["  "])["spurious"] == 0


def test_aggregate_reports_numerator_and_denominator() -> None:
    """비율이 아니라 분자/분모를 낸다(규약8)."""
    pos = [
        {"present": 2, "verbatim": 1, "expected": 1},
        {"present": 3, "verbatim": 3, "expected": 2},
    ]
    neg = [{"spurious": 1}]
    out = aggregate(pos, neg, n=3)
    assert out["present"] == {"numerator": 5, "denominator": 6}
    assert out["verbatim"] == {"numerator": 4, "denominator": 6}
    assert out["spurious"] == {"numerator": 1, "denominator": 3}


def test_run_end_to_end_with_scripted_llm_separates_the_two_defects() -> None:
    """추출 실패와 번안을 섞은 시나리오에서 축이 각각 다르게 움직인다."""
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases["positives"] = [
        {"caseId": "p1", "brand": "삼성", "utterance": "삼성 제품 아무거나"},
        {"caseId": "p2", "brand": "애플", "utterance": "애플 제품 아무거나"},
        {"caseId": "p3", "brand": "다이슨", "utterance": "다이슨 제품 아무거나"},
    ]
    cases["negatives"] = [{"caseId": "n1", "utterance": "이어폰 추천해줘"}]
    llm = _ScriptedLLM(
        {
            "삼성 제품 아무거나": ["삼성"],  # 정상
            "애플 제품 아무거나": ["Apple"],  # 번안 — present 만
            "다이슨 제품 아무거나": None,  # 추출 실패
            "이어폰 추천해줘": None,
        }
    )
    results = asyncio.run(run(llm, cases, n=1, tier="fast", concurrency=2))
    axes = results["axes"]
    assert axes["present"] == {"numerator": 2, "denominator": 3}
    assert axes["verbatim"] == {"numerator": 1, "denominator": 3}
    assert axes["expected"] == {"numerator": 1, "denominator": 3}
    assert axes["spurious"] == {"numerator": 0, "denominator": 1}


def test_run_counts_spurious_brand_on_negative_utterance() -> None:
    """엔진을 '무조건 브랜드를 낸다'로 변이시키면 spurious 가 실제로 오른다(공허성 방지)."""
    cases = {
        "datasetVersion": "t",
        "positives": [{"caseId": "p1", "brand": "삼성", "utterance": "삼성 제품 아무거나"}],
        "negatives": [{"caseId": "n1", "utterance": "이어폰 추천해줘"}],
    }
    llm = _ScriptedLLM({"삼성 제품 아무거나": ["삼성"], "이어폰 추천해줘": ["이어폰"]})
    results = asyncio.run(run(llm, cases, n=2, tier="fast", concurrency=2))
    assert results["axes"]["spurious"] == {"numerator": 2, "denominator": 2}


class _FlakyLLM:
    """앞 `fail_times` 회는 429 를 내고 그 뒤 정상 응답하는 LLM."""

    def __init__(self, fail_times: int, brand: list[str] | None, exc: Exception | None = None):
        self.remaining = fail_times
        self._brand = brand
        self._exc = exc or RuntimeError("Error code: 429 - rate_limit_exceeded")
        self.calls = 0

    async def complete(self, *, system: str, user: str, tier: str, max_tokens: int = 1024, **_):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self._exc
        return json.dumps({"intent": "recommend", "reply": "", "filters": {"brand": self._brand}})

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


def test_sample_retries_through_rate_limit() -> None:
    """429 는 재시도한다 — 없으면 org 공유 TPM 아래서 런 전체가 첫 429 에 죽는다."""
    from evals.filter_axes.brand_probe import _sample

    llm = _FlakyLLM(3, ["삼성"])
    slept: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        slept.append(seconds)

    got = asyncio.run(
        _sample(llm, "삼성 제품 아무거나", "fast", asyncio.Semaphore(1), sleep=_no_sleep)
    )
    assert got == ["삼성"]
    assert llm.calls == 4 and slept == [1, 2, 4]


def test_sample_does_not_swallow_non_rate_limit_errors() -> None:
    """429 외 오류를 삼키면 표본이 조용히 사라져 분모가 왜곡된다."""
    from evals.filter_axes.brand_probe import _sample

    llm = _FlakyLLM(1, ["삼성"], exc=RuntimeError("Error code: 500 - server_error"))
    with pytest.raises(RuntimeError, match="500"):
        asyncio.run(_sample(llm, "삼성 제품 아무거나", "fast", asyncio.Semaphore(1)))


def test_sample_gives_up_after_max_retries() -> None:
    """무한 재시도하지 않는다 — 상한에 닿으면 올려서 런이 조용히 늘어지지 않게."""
    from evals.filter_axes.brand_probe import _sample

    llm = _FlakyLLM(99, ["삼성"])

    async def _no_sleep(seconds: float) -> None:
        return None

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(
            _sample(
                llm,
                "삼성 제품 아무거나",
                "fast",
                asyncio.Semaphore(1),
                max_retries=3,
                sleep=_no_sleep,
            )
        )
    assert llm.calls == 3
