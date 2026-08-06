"""`evals/category_probe/manifest.py` 의 정본 지문 순수 함수 테스트 (이슈 #401).

`dictionary_fingerprint(dsn)` 은 DB 왕복이 필요해 통합 테스트 소관이다. 여기서는 DB 없이
검증 가능한 `fingerprint_rows`·`canonical_seed_fingerprint` 만 다룬다.
"""

from __future__ import annotations

import hashlib

from evals.category_probe.manifest import canonical_seed_fingerprint, fingerprint_rows

EXPECTED_LEAF_COUNT = 1007
EXPECTED_CODEPOINT_SHA256 = "db81e849616ec5782f9d1b4ecda1f6eb15f9dbc7a2ec939b40e33fa786d65089"


def test_fingerprint_rows_is_order_independent() -> None:
    """DB collation 순서와 정본 파일 순서가 달라도 같은 지문이 나와야 정본 대조가 의미 있다."""
    a = fingerprint_rows(["나 > 2", "가 > 1", "다 > 3"])
    b = fingerprint_rows(["다 > 3", "나 > 2", "가 > 1"])
    assert a == b


def test_fingerprint_rows_matches_manual_sha256() -> None:
    rows = ["가전 > TV", "PC부품 > CPU"]
    expected_sha256 = hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()
    assert fingerprint_rows(rows) == {"rowCount": 2, "sha256": expected_sha256}


def test_canonical_seed_fingerprint_matches_repo_file() -> None:
    fingerprint = canonical_seed_fingerprint()
    assert fingerprint["path"] == "db/catalog/seed/categories.json"
    assert fingerprint["rowCount"] == EXPECTED_LEAF_COUNT
    assert fingerprint["sha256"] == EXPECTED_CODEPOINT_SHA256
