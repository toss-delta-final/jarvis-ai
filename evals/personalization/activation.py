"""개인화 **활성화** 지표 — 프로필이 노출을 실제로 바꿨는가 (이슈 #482).

이득 지표(`pairedVsMemberNoProfile` 의 nDCG delta)만으로는 두 상태가 구분되지 않는다:
  (a) 프로필이 대부분의 턴에서 아무것도 바꾸지 않아 효과가 0에 가깝다
  (b) 프로필이 바꾸기는 하는데 좋은 방향이 아니다
둘의 처방이 정반대다 — (a)는 소비 방식(주입 지점·강도)을 고쳐야 하고, (b)는 프로필 내용을
고쳐야 한다. live-v1 산출물로 실측한 결과는 (b)였고(Δranking rate 58.1%), 그 판정을 매 run
에서 자동으로 얻기 위해 지표로 편입한다.

**순수 함수다** — LLM·파일·저장소 접근이 없다. 기존 산출물(`results.json`)만으로 재계산할 수
있어야 과거 baseline 을 소급 판정할 수 있다.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# 산출물 한 행을 짝짓는 키. **`caseId` 만으로 짝지으면 안 된다** — repeats>1 이면 같은 케이스가
# 여러 행이라 반복끼리 섞이고, 지터가 프로필 효과로 둔갑한다.
_PairKey = tuple[str, int]


def _by_pair_key(rows: Iterable[Mapping[str, Any]], *, side: str) -> dict[_PairKey, list[int]]:
    """행 목록을 `(caseId, repeat)` → 노출 id 목록으로. 중복 키는 예외다.

    중복을 마지막 값으로 덮으면 한쪽 결과가 **소리 없이** 사라지고 분모만 줄어든다. 지표가
    조용히 틀리는 것보다 실행이 멈추는 편이 낫다.
    """
    indexed: dict[_PairKey, list[int]] = {}
    for row in rows:
        key = (str(row["caseId"]), int(row.get("repeat", 0)))
        if key in indexed:
            raise ValueError(f"{side} 산출물에 중복 (caseId, repeat) 키가 있습니다: {key}")
        # 산출물 JSON 이 id 를 문자열로 실을 수 있다 — 타입 차이가 '변화' 로 오인되면 안 된다.
        indexed[key] = [int(product_id) for product_id in row.get("rankedProductIds") or []]
    return indexed


def ranking_change(
    baseline_rows: Iterable[Mapping[str, Any]],
    arm_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """baseline 대비 arm 의 노출 변화량 — Δranking rate.

    분류는 셋이다. **순서만 바뀐 것과 집합이 바뀐 것을 나누는 이유**는 개입 지점이 다르기
    때문이다: rerank 는 후보 안에서 상위 N 을 고르므로 순서를 바꾸면 절단선이 움직여 집합까지
    바뀔 수 있고, decompose 주입은 검색 필터 자체를 바꿔 집합을 통째로 갈아치운다. 두 값의
    비율이 개인화가 어느 층에서 작동했는지를 가리킨다.

    `bothEmpty`(양쪽 다 빈 노출)는 `same` 에 포함하되 **따로 센다** — "프로필이 안 바꿨다"가
    아니라 "비교할 노출이 없다"라, 검색 0건이 많은 데이터셋에서 changeRate 를 조용히 희석한다.
    분모에서 빼지 않는 이유는 `changeRate` 정의를 단순하게 유지하기 위해서이고, 대신 이 값을
    함께 실어 읽는 쪽이 보정할 수 있게 한다.

    짝이 없는 행은 계산에서 빼되 **개수를 남긴다** — 조용히 버리면 분모가 말없이 줄어든다.
    """
    baseline = _by_pair_key(baseline_rows, side="baseline")
    arm = _by_pair_key(arm_rows, side="arm")
    shared = sorted(set(baseline) & set(arm))

    same = order_only = set_changed = both_empty = 0
    for key in shared:
        left, right = baseline[key], arm[key]
        if not left and not right:
            both_empty += 1
            same += 1
        elif left == right:
            same += 1
        elif set(left) == set(right):
            order_only += 1
        else:
            set_changed += 1

    paired = len(shared)
    changed = order_only + set_changed
    return {
        "pairedCount": paired,
        "same": same,
        "orderOnly": order_only,
        "setChanged": set_changed,
        "bothEmpty": both_empty,
        # 짝이 0이면 비율은 0 이 아니라 **정의되지 않는다** — 0.0 으로 내면 "아무것도 안
        # 바뀌었다"로 읽혀 하네스 미실행과 활성화 0을 구분할 수 없다.
        "changeRate": (changed / paired) if paired else None,
        "unpairedBaseline": len(set(baseline) - set(arm)),
        "unpairedArm": len(set(arm) - set(baseline)),
    }
