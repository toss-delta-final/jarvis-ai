"""앵커 정답지 로더 — 해시 게이트 + 라이브 사전 pre-flight (#331)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.category_probe.schema import AnchorSet

FIXTURE_DIR = Path(__file__).with_name("fixtures")
DEFAULT_FIXTURE = "anchors.json"


def resolve_fixture_path(name: str, *, fixture_dir: Path | None = None) -> Path:
    """`default`(별칭) 또는 임의 경로를 앵커 파일 경로로 푼다.

    `fixture_dir` 가 명시됐고 `name` 이 상대 파일명이면 그 디렉터리 기준으로 푼다(테스트가
    `tmp_path` 격리 디렉터리를 가리킬 때 쓴다) — 절대경로나 `fixture_dir` 미지정 시에는 종전대로
    `name` 을 cwd 기준 경로로 그대로 쓴다(CLI `--fixture <path>` 규약).
    """
    directory = fixture_dir or FIXTURE_DIR
    if name == "default":
        return directory / DEFAULT_FIXTURE
    candidate = Path(name)
    if fixture_dir is not None and not candidate.is_absolute():
        return fixture_dir / candidate
    return candidate


def fixture_sha256(path: Path) -> str:
    """앵커 파일의 sha256 — 산출물에 그대로 실려 '무엇을 쟀는지'를 남긴다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_anchor_set(name: str = "default", *, fixture_dir: Path | None = None) -> AnchorSet:
    """앵커 파일을 읽는다. 커밋된 픽스처(`default`)는 manifest 해시와 **반드시** 대조한다.

    [F1-6] `default` 는 manifest.json 부재나 그 파일명 키 누락을 **거부**한다(ValueError) —
    종전엔 `manifest_path.exists()` 가 거짓이거나 `expected` 가 falsy(None)면 조용히 통과해,
    커밋된 앵커의 manifest 가 지워지거나 키가 빠져도 손상 탐지가 통째로 무력화됐다. `default`
    가 아닌 외부 경로(`--fixture <path>`, 후보 앵커 실험)는 **종전대로** 대조를 건너뛰되
    호출부가 `fixture_sha256` 으로 산출물에 해시를 남긴다 — 해시 검증은 "커밋된 정본"에만
    의미가 있고, 후보 실험 파일에 강제하면 정상적인 실험 워크플로가 막힌다.
    """
    path = resolve_fixture_path(name, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    is_default = name == "default"
    if not manifest_path.exists():
        if is_default:
            raise ValueError(
                f"기본 픽스처 manifest 가 없습니다: {manifest_path} — 커밋된 앵커는 sha256 대조가 "
                "필수입니다(재측정 없이 조용히 통과시키면 손상을 못 잡습니다)"
            )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected is None:
            if is_default:
                raise ValueError(
                    f"기본 픽스처 manifest 에 {path.name!r} 의 sha256 키가 없습니다 — "
                    "커밋된 앵커는 등재가 필수입니다"
                )
        elif expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return AnchorSet.model_validate_json(path.read_text(encoding="utf-8"))


class PreflightError(RuntimeError):
    """라이브 pg-catalog 와 앵커의 accept/absentKeyword 가 어긋난다 — 종료 코드 2 사유."""


def preflight_check_catalog(anchors: AnchorSet, dsn: str) -> None:
    """모든 accept 가 라이브 categories 에 실재하고, notInCatalog 표현은 실재하지 않는지 확인한다(§2b).

    거리 임계·앵커 라벨은 사전(taxonomy)에 종속된다(docs/lessons.md 2026-08-05) — 사전이 재시드돼
    앵커가 낡으면 이 pre-flight 가 조용한 오탐 대신 즉시 종료 코드 2 로 드러낸다. `--dry-run` 은
    pg 접근이 0 이어야 하므로 이 함수를 호출하지 않는다(cli.py 참조).
    """
    from app.pipelines.category_search import exact_lookup

    accepts: set[str] = set()
    for cell in anchors.cells:
        for expected_leg in cell.expected_legs:
            accepts.update(expected_leg.accept)
    if accepts:
        found = exact_lookup(sorted(accepts), dsn)
        missing = accepts - found
        if missing:
            raise PreflightError(
                f"accept 라벨 {sorted(missing)} 이 라이브 categories 사전에 없습니다 — "
                "사전이 재시드됐거나 앵커가 낡았습니다(재측정 필요, lessons 2026-08-05)"
            )

    absent_keywords = sorted({cell.absent_keyword for cell in anchors.cells if cell.absent_keyword})
    if absent_keywords:
        import psycopg

        # 잎(leaf) 부분만 본다 — canonical 은 "대분류 > 잎" 이라 대분류 이름에 우연히 그 부분
        # 문자열이 섞이면(예: "홈패브릭/수예" 대분류의 잎은 "방석"/"쿠션"뿐인데 대분류명이
        # '수예'를 포함) notInCatalog 를 오탐 거부한다. "이 개념을 가리키는 잎이 없다"가
        # notInCatalog 의 실제 주장이므로 잎에서만 ILIKE 한다.
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            for keyword in absent_keywords:
                cur.execute(
                    "SELECT category FROM categories "
                    "WHERE split_part(category, ' > ', 2) ILIKE %s LIMIT 1",
                    (f"%{keyword}%",),
                )
                row = cur.fetchone()
                if row is not None:
                    raise PreflightError(
                        f"notInCatalog 앵커 키워드 '{keyword}' 를 포함하는 잎이 라이브 사전에 "
                        f"실재합니다({row[0]!r}) — 이 셀은 더 이상 notInCatalog 가 아닙니다"
                        "(재측정 필요)"
                    )
