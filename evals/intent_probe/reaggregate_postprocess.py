"""P8 오프라인 재집계: intent_probe samples.csv를 다시 호출하지 않고 head 억제를 적용한다."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from app.agents.buyer.recommendation.leg_head import suppress_generic_single_leg
from app.agents.buyer.recommendation.state import CategoryQuery
from app.schemas.spring import ProductSearchFilters


def main(path: str) -> None:
    rows = list(csv.DictReader(Path(path).open()))
    counts = {"namedCategoryHasLeg": [0, 0], "conditionOnlyNoCategoryQuery": [0, 0]}
    for row in rows:
        group = row["group"]
        if group not in ("named_category", "condition_only"):
            continue
        raw = row["categoryLegs"]
        legs = [CategoryQuery(raw_category=None, query=raw.split("|", 1)[-1])] if raw else []
        after = suppress_generic_single_leg(legs, ProductSearchFilters(), enabled=True,
            generic_heads=frozenset({"거", "것", "상품", "제품", "아이템", "아무거나"}),
            condition_terms=frozenset({"무료배송", "가성비", "평점", "인기", "최저가"}))
        axis = "namedCategoryHasLeg" if group == "named_category" else "conditionOnlyNoCategoryQuery"
        counts[axis][1] += 1
        if bool(after) if axis == "namedCategoryHasLeg" else not bool(after):
            counts[axis][0] += 1
    for key, (num, den) in counts.items():
        print(f"{key}\t{num}/{den}\t{num / den:.4%}" if den else f"{key}\t0/0")


if __name__ == "__main__":
    main(sys.argv[1])
