#!/usr/bin/env python3
"""라이브 pg-catalog `color_synonyms` → repo 정본(색상 동의어 사전) 재생성 (이슈 #258).

`db/catalog/seed/color_synonyms.json`(789행 상세 배열)과
`db/catalog/init/05_color_synonyms_seed.sql`(부트스트랩 INSERT)을 같은 원천에서 함께 만든다.
둘 다 생성물 — 손으로 고치지 말고 이 스크립트로 재생성한다.

원천 I-17(Spring)이 도달 불가라 `color_synonyms` DB가 사실상 유일한 사본이다(패킷 §5).
그래서 이 스크립트는 `scripts/derive_category_seed.py`(오프라인 덤프 파일 파싱)와 달리
**라이브 DB**에서 직접 읽는다 — 파서가 아니라 psycopg 질의다.

기계 산출(DB의 term/canonical/provenance/doc_count)과 사람 검수 오버레이
(`db/catalog/seed/color_synonyms_review.json`)를 분리해 관리한다 — 오버레이가 기계 배정
위에 `status`/`canonical`/`provenance`를 확정한다. 자세한 규약은 `db/catalog/seed/README.md`
색상 동의어 절 참고. 새 파이썬 의존성을 추가하지 않는다(표준 라이브러리 + 이미 있는 psycopg).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_PATH = REPO_ROOT / "db" / "catalog" / "seed" / "color_synonyms_review.json"
DEFAULT_JSON_OUT = REPO_ROOT / "db" / "catalog" / "seed" / "color_synonyms.json"
DEFAULT_SQL_OUT = REPO_ROOT / "db" / "catalog" / "init" / "05_color_synonyms_seed.sql"

_SQL_BATCH_SIZE = 200


class SeedDeriveError(ValueError):
    """하네스트/오버레이 검증 실패 — 원인을 메시지에 명시한다."""


@dataclass(frozen=True)
class HarvestedRow:
    """DB `color_synonyms` 에서 그대로 읽은 기계 산출 행(검수 이전)."""

    term: str
    canonical: str | None
    provenance: str
    doc_count: int | None


@dataclass(frozen=True)
class SeedRow:
    """오버레이 적용 후 정본에 쓸 확정 행."""

    term: str
    canonical: str | None
    status: str
    provenance: str
    doc_count: int | None


@dataclass(frozen=True)
class ReviewOverlayEntry:
    term: str
    canonical: str | None
    note: str = ""


@dataclass(frozen=True)
class ReviewOverlay:
    approved: tuple[ReviewOverlayEntry, ...]
    rejected: tuple[ReviewOverlayEntry, ...]


def load_review_overlay(path: str | Path) -> ReviewOverlay:
    """검수 정본 JSON을 엄격 로드한다 — 형식 위반은 즉시 실패."""
    data = json.loads(Path(path).read_bytes().decode("utf-8"))
    if not isinstance(data, dict):
        raise SeedDeriveError(f"검수 오버레이는 객체여야 함: {path}")

    def _parse_entries(key: str, *, require_canonical: bool) -> tuple[ReviewOverlayEntry, ...]:
        raw = data.get(key)
        if not isinstance(raw, list):
            raise SeedDeriveError(f"검수 오버레이 '{key}'는 배열이어야 함: {path}")
        entries: list[ReviewOverlayEntry] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("term"), str):
                raise SeedDeriveError(f"검수 오버레이 '{key}' 항목에 문자열 term 없음: {item!r}")
            canonical = item.get("canonical")
            if require_canonical and not isinstance(canonical, str):
                raise SeedDeriveError(
                    f"검수 오버레이 '{key}' 항목 term={item['term']!r}에 문자열 canonical 없음"
                )
            if canonical is not None and not isinstance(canonical, str):
                raise SeedDeriveError(
                    f"검수 오버레이 '{key}' 항목 term={item['term']!r}의 canonical은 문자열/null이어야 함"
                )
            note = item.get("note", "")
            if not isinstance(note, str):
                raise SeedDeriveError(
                    f"검수 오버레이 '{key}' 항목의 note는 문자열이어야 함: {item!r}"
                )
            entries.append(ReviewOverlayEntry(item["term"], canonical, note))
        return tuple(entries)

    approved = _parse_entries("approved", require_canonical=True)
    rejected = _parse_entries("rejected", require_canonical=False)
    return ReviewOverlay(approved, rejected)


def _norm(value: str) -> str:
    """app.pipelines.color_synonyms._norm 과 같은 strip + casefold 정규화."""
    return value.strip().casefold()


def apply_overlay(harvested: list[HarvestedRow], overlay: ReviewOverlay) -> list[SeedRow]:
    """기계 산출 위에 검수 오버레이를 적용해 정본 행을 만든다.

    검증 순서는 실패 메시지가 원인을 바로 가리키도록 오버레이 내부 정합성 → 하네스트 대비
    존재성 → 의미 규칙(고아 승인·순환) 순으로 진행한다.
    """
    approved_terms = [entry.term for entry in overlay.approved]
    rejected_terms = [entry.term for entry in overlay.rejected]
    if len(approved_terms) != len(set(approved_terms)):
        dup = sorted({t for t in approved_terms if approved_terms.count(t) > 1})
        raise SeedDeriveError(f"오버레이 approved 안에 term 중복: {dup}")
    if len(rejected_terms) != len(set(rejected_terms)):
        dup = sorted({t for t in rejected_terms if rejected_terms.count(t) > 1})
        raise SeedDeriveError(f"오버레이 rejected 안에 term 중복: {dup}")
    intersection = set(approved_terms) & set(rejected_terms)
    if intersection:
        raise SeedDeriveError(f"오버레이 approved/rejected 교집합: {sorted(intersection)}")

    harvested_terms = {row.term for row in harvested}
    overlay_terms = set(approved_terms) | set(rejected_terms)
    missing_terms = overlay_terms - harvested_terms
    if missing_terms:
        raise SeedDeriveError(
            f"오버레이 term이 DB 수확 집합에 없음(재수확 불가 — 오타 의심): {sorted(missing_terms)}"
        )

    # DB 복원(05_color_synonyms_seed.sql 재적재) 경로로 들어온 과거 사람 검수 산출물
    # (provenance='human')이 오버레이에서 빠지면, canonical/provenance 가 되돌려진 적 없는
    # 사람 판단 그대로 pending_review 로 내려가 "검수 산출물인데 검수 대기"라는 모순 행이
    # 조용히 생긴다(#258 리뷰 F1). 오버레이가 모든 human 행을 명시적으로 덮게 강제한다.
    uncovered_human_terms = {
        row.term for row in harvested if row.provenance == "human" and row.term not in overlay_terms
    }
    if uncovered_human_terms:
        raise SeedDeriveError(
            "DB 에 남은 과거 검수 산출물(provenance='human')인데 오버레이가 덮지 않음: "
            f"{sorted(uncovered_human_terms)} — 승인을 철회하려면 오버레이에 이 term 을 "
            "approved(다른 canonical) 또는 rejected 로 명시해 기계 배정(canonical/provenance)을 "
            "어떻게 되돌릴지 밝혀라"
        )

    approved_by_term = {entry.term: entry for entry in overlay.approved}
    # load_review_overlay 가 approved 항목엔 문자열 canonical 을 이미 보장한다(require_canonical=True) —
    # 필터링 컴프리헨션으로 타입도 str 로 좁힌다.
    approved_canonicals: set[str] = {
        entry.canonical for entry in overlay.approved if entry.canonical is not None
    }
    missing_canonicals = approved_canonicals - harvested_terms
    if missing_canonicals:
        raise SeedDeriveError(
            f"오버레이 approved의 canonical이 DB 수확 집합에 없음: {sorted(missing_canonicals)}"
        )
    orphan_canonicals = approved_canonicals - set(approved_terms)
    if orphan_canonicals:
        raise SeedDeriveError(
            f"고아 승인 — canonical이 approved 자신은 아님: {sorted(orphan_canonicals)}"
        )
    for entry in overlay.approved:
        canonical = entry.canonical
        assert canonical is not None  # require_canonical=True 로 로드 시 이미 보장됨
        chain_target = approved_by_term[canonical].canonical
        if chain_target != canonical:
            raise SeedDeriveError(
                f"2단계 체인/순환 금지 위반: {entry.term!r} → {canonical!r} → {chain_target!r}"
            )

    rejected_by_term = {entry.term: entry for entry in overlay.rejected}
    rows: list[SeedRow] = []
    for row in sorted(harvested, key=lambda r: r.term):
        if row.term in approved_by_term:
            rows.append(
                SeedRow(
                    row.term,
                    approved_by_term[row.term].canonical,
                    "approved",
                    "human",
                    row.doc_count,
                )
            )
        elif row.term in rejected_by_term:
            rows.append(SeedRow(row.term, None, "rejected", "human", row.doc_count))
        else:
            rows.append(
                SeedRow(row.term, row.canonical, "pending_review", row.provenance, row.doc_count)
            )

    norm_seen: dict[str, str] = {}
    for row in rows:
        key = _norm(row.term)
        if key in norm_seen and norm_seen[key] != row.term:
            raise SeedDeriveError(f"_norm 기준 term 충돌: {norm_seen[key]!r} vs {row.term!r}")
        norm_seen[key] = row.term

    return rows


def fetch_harvested_rows(dsn: str) -> list[HarvestedRow]:
    """라이브 pg-catalog `color_synonyms`에서 기계 산출 열만 읽는다."""
    import psycopg  # noqa: PLC0415 - LAZY import(pg 미설치 환경 유닛테스트 회피)

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT term, canonical, provenance, doc_count FROM color_synonyms")
        return [
            HarvestedRow(term, canonical, provenance, doc_count)
            for term, canonical, provenance, doc_count in cur.fetchall()
        ]


def row_fingerprint(rows: list[SeedRow]) -> dict[str, object]:
    """codepoint 정렬(파이썬 `sorted()`) 순서의 전 필드 sha256 + 행 수.

    term뿐 아니라 canonical/status/provenance/doc_count 까지 지문에 넣는다 — 재수확으로
    doc_count·provenance만 바뀌어도(검수 status는 그대로여도) 드리프트를 잡아야 하기 때문이다.
    """
    ordered = sorted(rows, key=lambda r: r.term)
    lines = [
        f"{r.term}\t{r.canonical or ''}\t{r.status}\t{r.provenance}\t{r.doc_count if r.doc_count is not None else ''}"
        for r in ordered
    ]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"rowCount": len(rows), "sha256": digest}


def render_json(rows: list[SeedRow]) -> str:
    ordered = sorted(rows, key=lambda r: r.term)
    payload = [
        {
            "term": r.term,
            "canonical": r.canonical,
            "status": r.status,
            "provenance": r.provenance,
            "doc_count": r.doc_count,
        }
        for r in ordered
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _sql_literal(value: str | int | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return f"'{_sql_escape(value)}'"


def render_bootstrap_sql(rows: list[SeedRow]) -> str:
    """정본과 같은 원천(정렬된 행 목록)에서 부트스트랩 INSERT SQL을 생성한다."""
    ordered = sorted(rows, key=lambda r: r.term)
    header = f"""-- 색상 동의어 사전 부트스트랩 시드 (이슈 #258) — {len(ordered)}행, embedding 은 NULL.
--
-- ⚠️ 이 파일은 생성물이다 — 손으로 고치지 마라. `scripts/derive_color_synonym_seed.py` 로 재생성한다.
-- 정본은 `db/catalog/seed/color_synonyms.json`(같은 원천에서 파생) + 사람 검수 오버레이
-- `db/catalog/seed/color_synonyms_review.json`. 자세한 절차는 `db/catalog/seed/README.md`.
-- 여기서는 행만 만든다(embedding NULL) — 임베딩은 2단계 배치
-- (`app.pipelines.color_synonym_seed.seed_from_file`)가 채운다(04_categories_seed.sql 관례 그대로).
--
-- docker-entrypoint-initdb.d 는 컨테이너가 "완전히 새로" 뜰 때(빈 볼륨) 1회만 실행한다 —
-- 이미 만들어진 컨테이너의 볼륨에는 자동 반영되지 않는다. 기존 DB 에는 이렇게 수동 적용한다:
-- docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/05_color_synonyms_seed.sql
-- 재적재 시에는 검수로 승격된 status/canonical 등이 기존 행에도 반영돼야 하므로 충돌 행을
-- 정본 값으로 갱신한다. embedding/embedding_model 은 2단계 배치 소유이므로 절대 갱신하지 않는다.
"""
    blocks = [header]
    for batch_start in range(0, len(ordered), _SQL_BATCH_SIZE):
        batch = ordered[batch_start : batch_start + _SQL_BATCH_SIZE]
        values = ",\n".join(
            f"    ({_sql_literal(r.term)}, {_sql_literal(r.canonical)}, "
            f"{_sql_literal(r.status)}, {_sql_literal(r.provenance)}, {_sql_literal(r.doc_count)})"
            for r in batch
        )
        blocks.append(
            "\nINSERT INTO color_synonyms (term, canonical, status, provenance, doc_count) VALUES\n"
            f"{values}\nON CONFLICT (term) DO UPDATE SET\n"
            "    canonical = EXCLUDED.canonical,\n"
            "    status = EXCLUDED.status,\n"
            "    provenance = EXCLUDED.provenance,\n"
            "    doc_count = EXCLUDED.doc_count;\n"
        )
    return "".join(blocks)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "라이브 pg-catalog color_synonyms + 사람 검수 오버레이에서 repo 정본(색상 동의어 "
            "사전) JSON·부트스트랩 SQL 을 재생성한다."
        )
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="pg-catalog DSN (기본: app.core.config.get_settings().catalog_db_url)",
    )
    parser.add_argument(
        "--review",
        type=Path,
        default=DEFAULT_REVIEW_PATH,
        help=f"검수 오버레이 JSON 경로 (기본: {DEFAULT_REVIEW_PATH})",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_JSON_OUT,
        help=f"정본 JSON 출력 경로 (기본: {DEFAULT_JSON_OUT})",
    )
    parser.add_argument(
        "--sql-out",
        type=Path,
        default=DEFAULT_SQL_OUT,
        help=f"부트스트랩 SQL 출력 경로 (기본: {DEFAULT_SQL_OUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="쓰지 않고 커밋된 출력과 다르면 비0 종료 (재현 증명용)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dsn = args.dsn
    if dsn is None:
        from app.core.config import get_settings  # noqa: PLC0415 - lazy 설정 경로

        dsn = get_settings().catalog_db_url

    overlay = load_review_overlay(args.review)
    harvested = fetch_harvested_rows(dsn)
    rows = apply_overlay(harvested, overlay)
    fingerprint = row_fingerprint(rows)
    print(f"rowCount={fingerprint['rowCount']} codepoint sha256={fingerprint['sha256']}")

    json_content = render_json(rows)
    sql_content = render_bootstrap_sql(rows)

    if args.check:
        mismatches = []
        if not args.json_out.exists() or args.json_out.read_text(encoding="utf-8") != json_content:
            mismatches.append(str(args.json_out))
        if not args.sql_out.exists() or args.sql_out.read_text(encoding="utf-8") != sql_content:
            mismatches.append(str(args.sql_out))
        if mismatches:
            print(f"--check FAIL: 커밋본과 다름 — {mismatches}", file=sys.stderr)
            return 1
        print("--check OK: 커밋본과 바이트 동일")
        return 0

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json_content, encoding="utf-8")
    args.sql_out.parent.mkdir(parents=True, exist_ok=True)
    args.sql_out.write_text(sql_content, encoding="utf-8")
    print(f"wrote {args.json_out}")
    print(f"wrote {args.sql_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
