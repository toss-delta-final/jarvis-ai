"""카테고리 사전 정본(`db/catalog/seed/categories.json`) 불변식 + SQL 드리프트 테스트 (이슈 #401).

정본 JSON 파일과 그로부터 파생된 부트스트랩 SQL(`db/catalog/init/04_categories_seed.sql`)이
같은 원천에서 나왔는지를 커밋된 산출물 대 산출물로 대조한다. 덤프 파서 자체는
`test_derive_category_seed.py` 소관 — 여기는 repo 밖 덤프 파일에 의존하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.derive_category_seed import render_bootstrap_sql

REPO_ROOT = Path(__file__).resolve().parents[2]
JSON_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "categories.json"
SQL_PATH = REPO_ROOT / "db" / "catalog" / "init" / "04_categories_seed.sql"

# 리터럴로 박는다(§4.1) — 프로덕션 코드 상수를 그대로 읽어오면 "파일이 스스로와 같다"는
# 공허한 단언이 된다. 이 값은 이 이슈에서 실제로 계산·재현한 codepoint sha256 이다.
EXPECTED_LEAF_COUNT = 1007
EXPECTED_CODEPOINT_SHA256 = "db81e849616ec5782f9d1b4ecda1f6eb15f9dbc7a2ec939b40e33fa786d65089"


def _load_leaves() -> list[str]:
    return json.loads(JSON_PATH.read_bytes().decode("utf-8"))


def test_seed_json_has_expected_row_count() -> None:
    assert len(_load_leaves()) == EXPECTED_LEAF_COUNT


def test_seed_json_entries_are_all_strings() -> None:
    leaves = _load_leaves()
    assert all(isinstance(x, str) for x in leaves)


def test_seed_json_entries_have_no_duplicates() -> None:
    leaves = _load_leaves()
    assert len(leaves) == len(set(leaves))


def test_seed_json_entries_are_top_mid_leaf_pairs() -> None:
    """leaf 는 전부 "대분류 > 중분류" 형태 — `" > "` 를 포함해야 한다."""
    leaves = _load_leaves()
    assert all(" > " in name for name in leaves)


def test_seed_json_is_codepoint_sorted() -> None:
    """파이썬 `sorted()`(codepoint 오름차순) 그대로 저장돼 있어야 한다 — locale 무관 결정론."""
    leaves = _load_leaves()
    assert leaves == sorted(leaves)


def test_seed_json_codepoint_sha256_matches_recorded_fingerprint() -> None:
    """정본 지문이 이 이슈에서 실측한 값과 같은지 — 파일이 손으로 편집되면 여기서 잡힌다."""
    leaves = _load_leaves()
    digest = hashlib.sha256("\n".join(leaves).encode("utf-8")).hexdigest()
    assert digest == EXPECTED_CODEPOINT_SHA256


def test_bootstrap_sql_matches_json_source_byte_for_byte() -> None:
    """커밋된 JSON 에서 SQL 텍스트를 재생성해 커밋된 SQL 파일과 바이트 동일해야 한다.

    어긋나면(예: SQL 파일을 손으로 고쳤거나 재생성을 빠뜨렸으면) 실패한다 — "같은 원천에서
    생성했다"는 주장을 파일 대 파일로 검증한다.
    """
    leaves = _load_leaves()
    regenerated = render_bootstrap_sql(leaves)
    committed = SQL_PATH.read_bytes().decode("utf-8")
    assert regenerated == committed


def test_bootstrap_sql_contains_every_leaf_once() -> None:
    """SQL 문자열 값 개수가 leaf 개수와 같아야 한다(누락·중복 삽입 감지)."""
    leaves = _load_leaves()
    committed = SQL_PATH.read_bytes().decode("utf-8")
    assert committed.count("INSERT INTO categories (category) VALUES") >= 1
    assert committed.count("ON CONFLICT (category) DO NOTHING;") >= 1
    # 배치마다 한 줄에 값 하나 — 값 라인 수가 leaf 수와 같아야 한다.
    value_lines = [line for line in committed.splitlines() if line.strip().startswith("('")]
    assert len(value_lines) == len(leaves)
