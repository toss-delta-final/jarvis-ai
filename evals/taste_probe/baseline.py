"""trivial baseline (#462 §8, `evals/README.md` 1항 "trivial baseline 의무"의 이 하네스 몫).

정의: **"아무것도 안 뽑는다"**(프로덕션의 `profile_graph_delta_enabled=False` 근처). LLM 콜 0,
결정론 — 골든셋만으로 계산되고 러너 실행이 필요 없다(baseline 산출은 항상 빈 산출이라서).
같은 런의 같은 `report.md` 에 파이프라인 수치와 나란히 실어 "아무것도 안 하는 것보다 나은가"를
보인다.
"""

from __future__ import annotations

from typing import Any

from evals.taste_probe.schema import GoldenSet

_NOTE = (
    "baseline 은 오탐 축에서 정의상 완벽하고 미탐 축에서 정의상 최악이다 — 따라서 추출이 개선인지는"
    " 'recall 이 0보다 얼마나 큰가'를 '오탐을 얼마나 치렀나'와 함께 봐야 판정된다. 한 축만 보면"
    " baseline 을 이기는 것이 자명하거나 불가능해 보인다."
)


def score_baseline(golden_set: GoldenSet, *, n: int = 1) -> dict[str, Any]:
    """`n` 은 [A-5] `baselineNoiseFalsePositiveRate` 를 파이프라인 축(`noiseFalsePositiveRate`,
    분모=noise 세션×N)과 **같은 분모**로 대조하기 위해서만 쓴다 — baseline 자신은 여전히
    결정론 1회(LLM 콜 0)이지 N회 반복하는 것이 아니다.
    """
    non_noise = [session for session in golden_set.sessions if session.slice_id != "noise"]
    noise = [session for session in golden_set.sessions if session.slice_id == "noise"]
    total_expected = sum(len(session.expected_triples) for session in non_noise)
    total_sessions = len(golden_set.sessions)
    noise_denominator = len(noise) * n
    return {
        "baselineRecall": {
            "axisId": "baselineRecall",
            "title": "trivial baseline recall",
            "numerator": 0,
            "denominator": total_expected,
            "ratio": (0.0 / total_expected) if total_expected else None,
            "definition": {
                "numerator": "0(아무것도 안 뽑는다)",
                "denominator": "노이즈 제외 세션의 기대 트리플 총수",
            },
        },
        "baselineFalsePositiveRate": {
            "axisId": "baselineFalsePositiveRate",
            "title": "trivial baseline 오탐율",
            "numerator": 0,
            "denominator": 0,
            "ratio": None,
            "definition": {
                "numerator": "0(산출 트리플 자체가 없다)",
                "denominator": "baseline 산출 트리플 총수(정의상 항상 0)",
            },
        },
        # [A-5] 사전 등록 2차 축 `noiseFalsePositiveRate`(분모=noise 세션×N)와 나란히 대조할 수
        # 있는 baseline 값 — 기존 `baselineFalsePositiveRate`(0/0)는 분모 자체가 baseline 산출
        # 트리플 수라 파이프라인 축과 분모가 달라 직접 비교가 안 됐다.
        "baselineNoiseFalsePositiveRate": {
            "axisId": "baselineNoiseFalsePositiveRate",
            "title": "trivial baseline 오탐 슬라이스 산출 트리플 수",
            "numerator": 0,
            "denominator": noise_denominator,
            "ratio": (0.0 / noise_denominator) if noise_denominator else None,
            "definition": {
                "numerator": "0(아무것도 안 뽑는다)",
                "denominator": "noise 세션 수 × N — noiseFalsePositiveRate 와 같은 분모",
            },
        },
        "baselineSessionExactSet": {
            "axisId": "baselineSessionExactSet",
            "title": "trivial baseline 세션 집합 정확 일치",
            "numerator": len(noise),
            "denominator": total_sessions,
            "ratio": (len(noise) / total_sessions) if total_sessions else None,
            "definition": {
                "numerator": "기대 집합이 빈 세션(noise) 수 — baseline 출력([])과 우연히 일치",
                "denominator": "전체 세션 수",
            },
        },
        "note": _NOTE,
    }
