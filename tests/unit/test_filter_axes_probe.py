"""probe.py 유닛 테스트(#334) — fake LLM 주입, 실 LLM 호출 없음(규약 3).

INV 위반·DIR 위반(축 손실 + 후보 부분집합 위반)·pair leak(#119) 각 1건이 리포트에 드러나고
exit code 규약대로인지 검증한다.
"""

from __future__ import annotations

import json
import re

import pytest

from evals.filter_axes.cases import IdentityPair, IdentityRef, ProbeCase
from evals.filter_axes.probe import build_report, main, run_probe, write_artifacts
from evals.filter_axes.spec import load_axes_spec

SPEC = load_axes_spec()


def _fake_dataset_manifest(**overrides) -> dict:
    manifest = {
        "datasetVersion": "fax-test-0.0.0",
        "datasetHash": "f" * 64,
        "datasetHashVerified": True,
    }
    manifest.update(overrides)
    return manifest


def _decompose_payload(filters: dict) -> dict:
    return {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "semanticQuery": None,
        "categoryQueries": [],
        "filters": filters,
        "cart": None,
        "revertCategories": [],
        "repurchaseProducts": [],
    }


class FakeProbeLLM:
    """(query, profileSummary 유무) 쌍으로 스크립트된 decompose 산출을 낸다."""

    def __init__(self, scripts: dict[tuple[str, bool], dict]) -> None:
        self._scripts = scripts

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        del system, tier, max_tokens, json_output
        query_match = re.search(r"USER_MESSAGE: (.*)\Z", user, re.DOTALL)
        query = query_match.group(1) if query_match else ""
        has_profile = "PROFILE_SUMMARY: (없음)" not in user
        filters = self._scripts[(query, has_profile)]
        return json.dumps(_decompose_payload(filters), ensure_ascii=False)

    async def stream(self, *, system, user, tier, max_tokens=1024):
        del system, user, tier, max_tokens
        yield ""


class _FakeSettings:
    category_fanout_max = 5
    dedup_repurchase_max = 5


def _catalog() -> dict[str, dict]:
    return {
        "1": {
            "productId": 1,
            "name": "스테인리스 냄비 세트",
            "categoryName": "주방",
            "price": 30000,
        },
        "2": {"productId": 2, "name": "무선 이어폰", "categoryName": "가전", "price": 40000},
        "3": {"productId": 3, "name": "선크림 3개 세트", "categoryName": "뷰티", "price": 20000},
    }


@pytest.mark.asyncio
async def test_probe_reports_inv_violation_dir_violation_and_pair_leak() -> None:
    inv_case = ProbeCase(
        case_id="fax-inv-9001",
        kind="inv",
        base_query="이어폰 추천",
        variant_query="이어폰 추천해줘",
        variant_kind="honorific",
        notes="테스트",
    )
    dir_case = ProbeCase(
        case_id="fax-dir-9001",
        kind="dir",
        base_query="냄비 추천",
        variant_query="냄비 아무거나 추천",
        variant_kind="테스트 제약 제거",
        expected_new_axes=["price_max"],
        notes="테스트",
    )
    pair_case = ProbeCase(
        case_id="fax-pair-9001",
        kind="pair",
        base_query="선크림 추천",
        variant_query="선크림 추천",
        identity_pair=IdentityPair(
            guest=IdentityRef(kind="guest"),
            member=IdentityRef(kind="member", persona_id="persona-test"),
        ),
        notes="테스트",
    )

    scripts = {
        # INV: 존댓말 변형인데 color 축이 새로 붙는다 — 위반.
        ("이어폰 추천", False): {"keyword": "이어폰"},
        ("이어폰 추천해줘", False): {"keyword": "이어폰", "color": "빨강"},
        # DIR: variant 가 base 의 category 축을 잃고, 기대한 price_max 도 안 붙는다 — 위반.
        # (semanticQuery 는 decompose 가 항상 폴백을 채우므로 keyword 축은 축소 시나리오에
        # 못 쓴다 — category 로 검증한다.)
        ("냄비 추천", False): {"category": "주방"},
        ("냄비 아무거나 추천", False): {},
        # pair: 게스트는 키워드만, 회원은 프로필 탓에 priceMax 가 붙는다 — leak.
        ("선크림 추천", False): {"keyword": "선크림"},
        ("선크림 추천", True): {"keyword": "선크림", "priceMax": 50000},
    }
    llm = FakeProbeLLM(scripts)

    results = await run_probe(
        llm,
        [inv_case, dir_case, pair_case],
        settings=_FakeSettings(),
        spec=SPEC,
        catalog=_catalog(),
        purchase_history={
            "persona-test": {
                "orders": [
                    {
                        "items": [
                            {"categoryName": "뷰티", "price": 20000},
                        ]
                    }
                ]
            }
        },
    )
    dataset_manifest = _fake_dataset_manifest()
    report = build_report(results, spec=SPEC, dataset_manifest=dataset_manifest)

    assert report["violationCount"] == 3
    assert report["violationCaseIds"] == ["fax-dir-9001", "fax-inv-9001", "fax-pair-9001"]
    assert report["caseIds"] == ["fax-dir-9001", "fax-inv-9001", "fax-pair-9001"]
    assert report["probeDatasetVersion"] == "fax-test-0.0.0"
    assert report["probeDatasetHash"] == "f" * 64
    assert report["probeDatasetHashVerified"] is True

    by_id = {row["caseId"]: row for row in results}
    assert by_id["fax-inv-9001"]["verdict"]["brokenAxes"] == [{"axis": "color", "kind": "added"}]
    assert by_id["fax-dir-9001"]["verdict"]["lostAxes"] == ["category"]
    assert by_id["fax-dir-9001"]["verdict"]["missingNewAxes"] == ["price_max"]
    assert by_id["fax-dir-9001"]["candidateSubset"]["ok"] is False
    assert by_id["fax-pair-9001"]["verdict"] == {
        "leak": True,
        "leakedAxes": ["price_max"],
        "lostAxes": [],
    }


@pytest.mark.asyncio
async def test_probe_reports_no_violations_when_all_judges_pass() -> None:
    inv_case = ProbeCase(
        case_id="fax-inv-9002",
        kind="inv",
        base_query="이어폰 추천",
        variant_query="이어폰 추천해줘",
        variant_kind="honorific",
        notes="테스트",
    )
    scripts = {
        ("이어폰 추천", False): {"keyword": "이어폰"},
        ("이어폰 추천해줘", False): {"keyword": "이어폰"},
    }
    llm = FakeProbeLLM(scripts)

    results = await run_probe(
        llm,
        [inv_case],
        settings=_FakeSettings(),
        spec=SPEC,
        catalog=_catalog(),
        purchase_history={},
    )
    report = build_report(results, spec=SPEC, dataset_manifest=_fake_dataset_manifest())

    assert report["violationCount"] == 0
    assert report["violationCaseIds"] == []


@pytest.mark.asyncio
async def test_write_artifacts_embeds_axes_spec_and_probe_dataset_metadata(tmp_path) -> None:
    """리뷰 F3+F4 — axes.json 동봉과 probe 데이터셋 버전/해시/caseIds가 results.json·
    run_manifest.json에 실제 값으로 실리는지 fake LLM 경로로 검증한다(실 LLM 호출 없음)."""
    import hashlib

    inv_case = ProbeCase(
        case_id="fax-inv-9003",
        kind="inv",
        base_query="이어폰 추천",
        variant_query="이어폰 추천해줘",
        variant_kind="honorific",
        notes="테스트",
    )
    scripts = {
        ("이어폰 추천", False): {"keyword": "이어폰"},
        ("이어폰 추천해줘", False): {"keyword": "이어폰"},
    }
    llm = FakeProbeLLM(scripts)

    results = await run_probe(
        llm,
        [inv_case],
        settings=_FakeSettings(),
        spec=SPEC,
        catalog=_catalog(),
        purchase_history={},
    )
    dataset_manifest = _fake_dataset_manifest(datasetVersion="fax-9.9.9", datasetHash="c" * 64)
    report = build_report(results, spec=SPEC, dataset_manifest=dataset_manifest)
    run_manifest = {
        "run": {"runId": "x", "timestamp": "t", "command": "c"},
        "filterAxesProbe": {
            "modelConfig": {"provider": "fake"},
            "caseCount": 1,
            "caseIds": [inv_case.case_id],
            "datasetVersion": dataset_manifest["datasetVersion"],
            "datasetHash": dataset_manifest["datasetHash"],
            "datasetHashVerified": dataset_manifest["datasetHashVerified"],
        },
    }
    output = tmp_path / "probe-out"

    write_artifacts(output, report, run_manifest)

    written_results = json.loads((output / "results.json").read_text(encoding="utf-8"))
    written_manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))

    assert written_results["probeDatasetVersion"] == "fax-9.9.9"
    assert written_results["probeDatasetHash"] == "c" * 64
    assert written_results["probeDatasetHashVerified"] is True
    assert written_results["caseIds"] == ["fax-inv-9003"]
    assert written_manifest["filterAxesProbe"]["datasetVersion"] == "fax-9.9.9"
    assert written_manifest["filterAxesProbe"]["datasetHash"] == "c" * 64
    assert written_manifest["filterAxesProbe"]["caseIds"] == ["fax-inv-9003"]

    spec_bytes = (output / "filter_axes_spec.json").read_bytes()
    assert hashlib.sha256(spec_bytes).hexdigest() == written_results["axesSpecSha256"]


def test_main_rejects_when_out_dir_already_exists(tmp_path) -> None:
    existing = tmp_path / "already-there"
    existing.mkdir()
    assert main(["--out", str(existing)]) == 2


def test_main_rejects_unknown_case_ids(tmp_path) -> None:
    out = tmp_path / "out"
    assert main(["--out", str(out), "--case-ids", "no-such-case"]) == 2
    assert not out.exists()
