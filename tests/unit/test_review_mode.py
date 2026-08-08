"""이슈 #457 — Claude PR Review 모드(full/skip/incremental/integration) 판별 테스트.

`.github` 는 파이썬 패키지가 아니므로 `importlib.util.spec_from_file_location` 으로
`.github/scripts/review_mode.py` 를 직접 로드한다(`test_check_spring_connection_script.py` 의
`scripts/` 패키지 import 관례는 여기 적용할 수 없다 — `.github` 는 유효한 식별자가 아니다).

시나리오 3/4/5/7/8 은 임시 git 저장소를 실제로 만들어(`tmp_path` + `subprocess`) 돌린다 —
`decide_mode` 에 손으로 만든 dict 를 넣는 것만으로는 merge-base 이동·hunk 재계산 같은
git 고유 동작을 검증하지 못한다.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "review_mode.py"
)
_spec = importlib.util.spec_from_file_location("review_mode", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
review_mode = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = review_mode  # dataclass 는 __module__ 을 sys.modules 에서 찾는다
_spec.loader.exec_module(review_mode)


# ── git 저장소 헬퍼 ──
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        p = repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    _write(repo, files)
    for rel_path in files:
        _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _set_origin_dev(repo: Path, sha: str) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/dev", sha)


def _merge_dev_into_pr(repo: Path, message: str = "merge dev") -> str:
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", "-m", message, "dev")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _full_review_state(
    repo: Path, base_sha: str, head_sha: str, base_tip: str | None = None
) -> dict:
    """마지막 성공 리뷰 상태를 만든다. `base_tip` 은 그 시점의 origin/<base_ref> tip 이다(F1) —
    대부분의 시나리오에서는 review 시점에 base_sha(merge-base)와 tip 이 같으므로 생략 가능하다.
    """
    patch = review_mode.git_diff(repo, base_sha, head_sha)
    return {
        "version": 1,
        "reviewed_head": head_sha,
        "reviewed_base": base_sha,
        "reviewed_base_tip": base_tip if base_tip is not None else base_sha,
        "patch_id": review_mode.git_patch_id(repo, patch),
        "patch_sha256": "deadbeef",
        "file_fingerprints": review_mode.file_fingerprints(patch),
        "mode": "full",
        "run_id": "1",
        "reviewed_at": "2026-08-01T00:00:00+00:00",
    }


def _detect(repo: Path, *, event_action: str, head_sha: str, state: dict | None):
    body = review_mode.render_state_comment(state) if state is not None else None
    return review_mode.detect_review_context(
        repo,
        event_action=event_action,
        base_ref="dev",
        head_sha=head_sha,
        state_comment_body=body,
    )


FOO_V1 = "def a():\n    return 1\n"
FOO_V2 = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


# ── 1. opened → full ──
def test_opened_event_is_always_full(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    ctx = _detect(repo, event_action="opened", head_sha=head1, state=None)

    assert ctx.mode == "full"
    assert ctx.skip_reason == "opened"
    assert ctx.budget == review_mode.FULL_BUDGET


# ── 2. state 없음/JSON 깨짐/reviewed_head 없음 → full ──
def test_missing_state_comment_is_full(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    ctx = _detect(repo, event_action="synchronize", head_sha=head1, state=None)

    assert ctx.mode == "full"
    assert ctx.skip_reason == "no_state"


def test_corrupt_state_json_is_full(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    broken_body = f"{review_mode.STATE_MARKER}\n```json\n{{not valid json\n```\n"
    ctx = review_mode.detect_review_context(
        repo,
        event_action="synchronize",
        base_ref="dev",
        head_sha=head1,
        state_comment_body=broken_body,
    )

    assert ctx.mode == "full"
    assert ctx.skip_reason == "corrupt_state"


def test_reviewed_head_missing_from_repo_is_full(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    fake_state = {
        "version": 1,
        "reviewed_head": "f" * 40,
        "reviewed_base": base0,
        "reviewed_base_tip": base0,
        "file_fingerprints": {},
        "patch_id": "x",
        "patch_sha256": "y",
        "mode": "full",
        "run_id": "1",
        "reviewed_at": "2026-08-01T00:00:00+00:00",
    }
    ctx = _detect(repo, event_action="synchronize", head_sha=head1, state=fake_state)

    assert ctx.mode == "full"
    assert ctx.skip_reason == "stale_state"


# ── 3. dev 동기화만, PR이 안 건드린 파일만 base 에서 변경 → skip ──
def test_unrelated_dev_sync_is_skip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1, "app/bar.py": "z = 1\n"}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")
    state = _full_review_state(repo, base0, head1)

    _git(repo, "checkout", "-q", "dev")
    dev1 = _commit(repo, {"app/bar.py": "z = 2\n"}, "unrelated dev sync")
    _set_origin_dev(repo, dev1)
    head2 = _merge_dev_into_pr(repo)

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode == "skip"
    assert ctx.skip_reason == "patch_identical"


# ── 4. PR 자체 수정 → incremental, target diff 에 그 파일만, dev 변경 파일 없음 ──
def test_pr_self_edit_is_incremental_with_scoped_target(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1, "app/bar.py": "z = 1\n"}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")
    state = _full_review_state(repo, base0, head1)

    head2 = _commit(repo, {"app/foo.py": FOO_V2 + "\ndef c():\n    return 3\n"}, "review fix")

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode == "incremental"
    assert ctx.target_files == frozenset({"app/foo.py"})
    assert "app/bar.py" not in ctx.target_diff


# ── 5. PR 파일을 dev 도 변경 + 머지 → integration, base-context.diff 는 overlap 파일만 ──
def _build_integration_scenario(tmp_path: Path):
    """시나리오 5(dev 가 PR 파일을 건드려 integration)의 ctx 를 만든다.

    dev 는 PR 이 안 건드린 a() 만 바꾸고 PR 은 b() 만 바꿔, 20줄 떨어진 hunk 라 merge 는
    충돌 없이 합쳐진다 — merge 후 app/foo.py 의 base 대비 diff(b() 변경분)는 review 이전과
    내용이 같아, `changed_files` 는 비고 `overlap` 만 {app/foo.py} 인 integration 이 나온다.
    """
    repo = _init_repo(tmp_path)
    base0 = _commit(
        repo,
        {
            "app/foo.py": "def a():\n    return 1\n" + "\n" * 20 + "def b():\n    return 2\n",
            "app/bar.py": "z = 1\n",
        },
        "init",
    )
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(
        repo,
        {
            "app/foo.py": (
                "def a():\n    return 1\n" + "\n" * 20 + "def b():\n    return 20\n"
            )
        },
        "pr changes b()",
    )
    state = _full_review_state(repo, base0, head1)

    _git(repo, "checkout", "-q", "dev")
    dev1 = _commit(
        repo,
        {
            "app/foo.py": "def a():\n    return 10\n" + "\n" * 20 + "def b():\n    return 2\n",
            "app/bar.py": "z = 2\n",
        },
        "dev changes a() and bar.py",
    )
    _set_origin_dev(repo, dev1)
    head2 = _merge_dev_into_pr(repo)

    return _detect(repo, event_action="synchronize", head_sha=head2, state=state)


def test_dev_touches_pr_file_and_merges_is_integration(tmp_path: Path) -> None:
    ctx = _build_integration_scenario(tmp_path)

    assert ctx.mode == "integration"
    assert ctx.target_files == frozenset({"app/foo.py"})
    assert "app/bar.py" not in ctx.base_context_diff
    assert "app/foo.py" in ctx.base_context_diff


# ── R2 (리뷰 라운드 1) — build_observation 은 mode 에서 역산하지 않고 decide_mode 의 실제
# 판정 결과(changed_files/overlap)를 그대로 옮겨야 한다. integration 은 overlap 우선 판정이라
# patch 가 동일해도(이 시나리오처럼 changed_files=∅) 발생할 수 있다 — mode 로부터
# patch_changed 를 역산하면 이 케이스에서 "patch_changed=true" 라고 거짓말한다.
def test_build_observation_reflects_decision_not_mode_for_integration(tmp_path: Path) -> None:
    ctx = _build_integration_scenario(tmp_path)
    assert ctx.mode == "integration"

    observation = review_mode.build_observation(ctx)

    assert observation["patch_changed"] == "false"
    assert observation["changed_files_since_review"] == "(none)"
    assert observation["integration_related_files"] == "app/foo.py"
    assert observation["target_files"] == "app/foo.py"


# ── 6. file_fingerprints 는 hunk 위치를 보존해야 한다 (F5, 순수 함수 직접 검증) ──
# 라운드 1(R2)에서는 "base 이동으로 hunk 위치만 밀린 경우"를 위해 hunk 줄 번호를 지웠었다.
# 하지만 그 상황은 dev 가 같은 파일을 건드렸다는 뜻이라 §2.3 의 overlap 정의
# (base_files ∩ pr_files, path 기준)로 **항상 먼저** integration 으로 잡힌다 — 정규화가 실제
# 모드 판정을 한 번도 바꾸지 못했다. 반대로 정규화는 "같은 변경 내용이 의미가 다른 위치로
# 옮겨진 진짜 변경"(반복되는 설정/테이블에서 앞뒤 3줄 컨텍스트가 우연히 같은 경우)을
# "변경 없음"으로 오판시켜 false skip 만 만든다. 그래서 F5 로 정규화를 없앴다 — 같은 내용이라도
# hunk 위치가 다르면 지문도 달라야 한다(리뷰를 한 번 더 도는 쪽이 안전하다).
def test_file_fingerprints_differ_when_hunk_position_differs() -> None:
    patch_a = (
        "diff --git a/app/foo.py b/app/foo.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/foo.py\n"
        "+++ b/app/foo.py\n"
        "@@ -10,3 +10,4 @@ def a():\n"
        "     return 1\n"
        "+    # note\n"
    )
    patch_b = (
        "diff --git a/app/foo.py b/app/foo.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/foo.py\n"
        "+++ b/app/foo.py\n"
        "@@ -55,3 +55,4 @@ def a():\n"
        "     return 1\n"
        "+    # note\n"
    )
    fp_a = review_mode.file_fingerprints(patch_a)
    fp_b = review_mode.file_fingerprints(patch_b)
    assert fp_a != fp_b


# ── F4 — 바이너리 diff 는 index SHA 가 유일한 내용 신호라 정규화하면 안 된다 ──
def test_file_fingerprints_differ_for_different_binary_content() -> None:
    patch_a = (
        "diff --git a/app/y.bin b/app/y.bin\n"
        "index c866266..5663091 100644\n"
        "Binary files a/app/y.bin and b/app/y.bin differ\n"
    )
    patch_b = (
        "diff --git a/app/y.bin b/app/y.bin\n"
        "index c866266..9401fc7 100644\n"
        "Binary files a/app/y.bin and b/app/y.bin differ\n"
    )
    fp_a = review_mode.file_fingerprints(patch_a)
    fp_b = review_mode.file_fingerprints(patch_b)
    assert fp_a != fp_b


def test_file_fingerprints_normalize_index_sha_for_text_diff_only() -> None:
    # 텍스트 diff 는 여전히 index SHA 를 정규화한다(무관한 다른 부분 변경으로 인한 오탐 방지).
    text_a = (
        "diff --git a/app/foo.py b/app/foo.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/foo.py\n"
        "+++ b/app/foo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    text_b = text_a.replace("index 1111111..2222222", "index 3333333..4444444")
    assert review_mode.file_fingerprints(text_a) == review_mode.file_fingerprints(text_b)


# ── 7. 공백/들여쓰기만 바뀐 경우 → skip 이 아니어야 한다 (--verbatim 회귀 방지) ──
def test_whitespace_only_change_is_not_skip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": "def a():\n  return 1\n"}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": "def a():\n  return 1\n\n\ndef b():\n  return 2\n"}, "pr change")
    state = _full_review_state(repo, base0, head1)

    head2 = _commit(
        repo, {"app/foo.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n"}, "reindent"
    )

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode != "skip"
    assert ctx.mode == "incremental"


# ── 8. *.md/docs/** 만 겹친 dev 변경 → integration 이 아니라 skip (리뷰 범위 필터) ──
def test_docs_only_overlap_is_skip_not_integration(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # 위/아래로 충분히 떨어뜨려 PR 삽입(위)과 dev 삽입(아래)이 3-way merge 시 충돌하지 않게 한다.
    base_changelog = "\n".join(f"L{i}" for i in range(20)) + "\n"
    pr_changelog = "\n".join(["L0", "- pr entry", *[f"L{i}" for i in range(1, 20)]]) + "\n"
    dev_changelog = "\n".join([*[f"L{i}" for i in range(20)], "- dev entry"]) + "\n"

    base0 = _commit(repo, {"app/foo.py": FOO_V1, "CHANGELOG.md": base_changelog}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(
        repo, {"app/foo.py": FOO_V2, "CHANGELOG.md": pr_changelog}, "pr change"
    )
    state = _full_review_state(repo, base0, head1)

    _git(repo, "checkout", "-q", "dev")
    dev1 = _commit(repo, {"CHANGELOG.md": dev_changelog}, "dev changelog only")
    _set_origin_dev(repo, dev1)
    head2 = _merge_dev_into_pr(repo)

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode == "skip"
    assert ctx.skip_reason == "patch_identical"


# ── F1 — 통합 판정은 merge-base 가 아니라 base 브랜치 tip 기준이어야 한다 ──
# merge-base(origin/dev, head) 는 PR 이 dev 를 머지하지 않는 한 dev 가 아무리 전진해도
# 그대로다. 그래서 "dev 가 PR 파일을 바꿨지만 PR 은 머지하지 않고 다른 파일만 고쳐 push" 하면
# 옛 merge-base 판정은 base_changed=false 로 보고 skip 으로 새 버린다 — 실제로는 GitHub 이
# 머지할 결과(PR head + dev tip)가 리뷰 안 된 상태다.
def test_dev_advances_and_touches_pr_file_without_pr_merging_is_integration(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1, "app/bar.py": "z = 1\n"}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")
    state = _full_review_state(repo, base0, head1)

    # dev 가 PR 이 리뷰받은 파일(app/foo.py)을 더 고친다 — PR 은 이걸 머지하지 않는다.
    _git(repo, "checkout", "-q", "dev")
    dev1 = _commit(repo, {"app/foo.py": FOO_V2 + "\n# dev tweak\n"}, "dev touches pr file")
    _set_origin_dev(repo, dev1)

    # PR 은 dev1 을 머지하지 않고, 무관한 파일만 고쳐 push (synchronize).
    _git(repo, "checkout", "-q", "pr")
    head2 = _commit(repo, {"app/bar.py": "z = 2\n"}, "pr unrelated fix")

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode == "integration"
    assert "app/foo.py" in ctx.base_context_diff


def test_dev_advances_unrelated_file_without_pr_merging_still_skip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(
        repo, {"app/foo.py": FOO_V1, "app/baz.py": "y = 1\n"}, "init"
    )
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")
    state = _full_review_state(repo, base0, head1)

    # dev 는 PR 이 전혀 건드리지 않은 파일만 고친다 — PR 은 머지하지 않는다.
    _git(repo, "checkout", "-q", "dev")
    dev1 = _commit(repo, {"app/baz.py": "y = 2\n"}, "dev unrelated")
    _set_origin_dev(repo, dev1)

    # PR 자체는 새로 push 할 게 없다(head1 그대로) — base tip 만 움직인 재실행 시나리오.
    ctx = _detect(repo, event_action="synchronize", head_sha=head1, state=state)

    assert ctx.mode == "skip"
    assert ctx.skip_reason == "patch_identical"


def test_dev_only_big_change_yields_integration_with_escalated_budget_and_prompt_note(
    tmp_path: Path,
) -> None:
    """F1 파생효과 2개(budget 합산·프롬프트 note)를 end-to-end 로 잰다.

    `count_changed_lines(target_diff) + count_changed_lines(base_context_diff)` 를 테스트가
    직접 계산해 `compute_budget`/`build_prompt` 에 넣는 단위 테스트만으로는, `detect_review_
    context` 안에서 실제로 그 값을 계산해 넘기는 배선이 사라져도 통과한다(리뷰 라운드 4 T1
    실측 — 아래 시나리오로 두 변이(base-context 합산 줄 제거 / target_diff_empty 를 False 로
    고정)에서 실제로 깨지는 것을 확인했다). 시나리오: PR 이 리뷰받은 파일을 dev 가 크게(budget
    승급 임계 초과) 고치고 tip 만 전진 — PR 은 머지하지 않고 리뷰 범위 밖(*.md) 파일만 push 해,
    PR 쪽 리뷰 대상(target.diff)은 그대로 비어 있다.
    """
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")
    state = _full_review_state(repo, base0, head1)

    # dev 가 app/foo.py 를 크게(변경 줄 수 > BUDGET_ESCALATE_LINES=150, <= BUDGET_MAX_LINES=500)
    # 고치고 tip 을 전진시킨다. PR 은 이걸 머지하지 않는다.
    _git(repo, "checkout", "-q", "dev")
    big_change = "\n".join(f"line{i}" for i in range(200)) + "\n"
    dev1 = _commit(repo, {"app/foo.py": big_change}, "dev big change to pr file")
    _set_origin_dev(repo, dev1)

    # PR 은 리뷰 범위 밖(*.md) 파일만 바꿔 push — PR 쪽 리뷰 대상 자체는 head1 과 동일하다.
    _git(repo, "checkout", "-q", "pr")
    head2 = _commit(repo, {"NOTES.md": "무관 메모\n"}, "pr docs-only push")

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode == "integration"
    assert ctx.changed_files == frozenset()
    assert ctx.target_diff.strip() == ""
    assert ctx.budget == 80  # base-context.diff 줄 수가 반영되지 않으면 60 이 나온다
    assert "PR 쪽 변경은 없고" in ctx.prompt


# 아래 두 테스트는 detect_review_context 배선이 아니라 compute_budget/build_prompt 자체의
# 순수 함수 계약(입력이 주어지면 무엇을 내는지)만 고정한다 — 위 end-to-end 테스트가 실제
# 배선을 검증한다.
def test_integration_budget_counts_base_context_diff_lines() -> None:
    target_diff = ""
    base_context_diff = "\n".join(f"+line{i}" for i in range(200))
    changed_line_count = review_mode.count_changed_lines(
        target_diff
    ) + review_mode.count_changed_lines(base_context_diff)
    assert changed_line_count == 200
    assert review_mode.compute_budget("integration", changed_line_count, 1, ["app/foo.py"]) == 80


def test_build_prompt_appends_note_when_integration_target_diff_empty() -> None:
    note_phrase = "PR 쪽 변경은 없고"
    prompt = review_mode.build_prompt("integration", target_diff_empty=True)
    assert note_phrase in prompt
    prompt_normal = review_mode.build_prompt("integration", target_diff_empty=False)
    assert note_phrase not in prompt_normal


# ── 9. budget 계층 ──
@pytest.mark.parametrize(
    ("mode", "lines", "files", "target_files", "expected"),
    [
        ("incremental", 10, 1, ["app/foo.py"], 40),
        ("incremental", 151, 1, ["app/foo.py"], 60),
        ("incremental", 10, 4, ["app/foo.py"], 60),
        ("incremental", 501, 1, ["app/foo.py"], 100),
        ("incremental", 10, 9, ["app/foo.py"], 100),
        ("integration", 10, 1, ["app/foo.py"], 60),
        ("integration", 151, 1, ["app/foo.py"], 80),
        ("integration", 10, 4, ["app/foo.py"], 80),
        ("integration", 501, 1, ["app/foo.py"], 100),
        ("integration", 10, 9, ["app/foo.py"], 100),
    ],
)
def test_budget_tiers(mode, lines, files, target_files, expected) -> None:
    assert review_mode.compute_budget(mode, lines, files, target_files) == expected


def test_budget_escalates_to_max_for_high_impact_path() -> None:
    # 작은 1줄 변경이라도 계약 표면(app/schemas/**)은 최고 단계로 승급한다.
    assert review_mode.compute_budget("incremental", 1, 1, ["app/schemas/x.py"]) == 100
    assert review_mode.compute_budget("integration", 1, 1, ["app/core/config.py"]) == 100


# ── 10. is_review_successful ──
def test_is_review_successful_ok_result() -> None:
    content = json.dumps([{"type": "result", "subtype": "success", "is_error": False}])
    ok, _ = review_mode.is_review_successful("success", content)
    assert ok is True


# R3 (리뷰 라운드 1) — 마지막 원소만 보면 result 뒤에 메시지가 하나라도 더 붙을 때(예: 액션이
# 종료 정리용 system 메시지를 덧붙이는 경우) 성공한 리뷰가 매번 "미완료"로 오판되어 state 가
# 영영 갱신되지 않는다(영구 full 리뷰). 뒤에서부터 type=="result" 인 마지막 메시지를 찾아야 한다.
def test_is_review_successful_ok_when_trailing_message_after_result() -> None:
    content = json.dumps(
        [
            {"type": "result", "subtype": "success", "is_error": False},
            {"type": "system"},
        ]
    )
    ok, reason = review_mode.is_review_successful("success", content)
    assert ok is True
    assert reason == "ok"


def test_is_review_successful_error_max_turns() -> None:
    content = json.dumps([{"type": "result", "subtype": "error_max_turns", "is_error": True}])
    ok, reason = review_mode.is_review_successful("success", content)
    assert ok is False
    assert "result_subtype" in reason


def test_is_review_successful_error_during_execution() -> None:
    content = json.dumps(
        [{"type": "result", "subtype": "error_during_execution", "is_error": True}]
    )
    ok, _ = review_mode.is_review_successful("success", content)
    assert ok is False


def test_is_review_successful_empty_file() -> None:
    ok, reason = review_mode.is_review_successful("success", "")
    assert ok is False
    assert reason == "execution_file_missing_or_empty"


def test_is_review_successful_broken_json() -> None:
    ok, reason = review_mode.is_review_successful("success", "{not json")
    assert ok is False
    assert "execution_file_parse_error" in reason


def test_is_review_successful_missing_file() -> None:
    ok, reason = review_mode.is_review_successful("success", None)
    assert ok is False
    assert reason == "execution_file_missing_or_empty"


def test_is_review_successful_step_outcome_failure() -> None:
    ok, reason = review_mode.is_review_successful("failure", None)
    assert ok is False
    assert reason == "claude_step_outcome=failure"


# ── 11. parse/render 왕복, 마커 없는 코멘트 무시, 60000자 초과 시 file_fingerprints 생략 ──
def test_state_comment_roundtrip() -> None:
    state = {
        "version": 1,
        "reviewed_head": "a" * 40,
        "reviewed_base": "b" * 40,
        "reviewed_base_tip": "b" * 40,
        "patch_id": "c" * 40,
        "patch_sha256": "d" * 64,
        "file_fingerprints": {"app/foo.py": "e" * 64},
        "mode": "full",
        "run_id": "42",
        "reviewed_at": "2026-08-01T00:00:00+00:00",
    }
    body = review_mode.render_state_comment(state)
    parsed = review_mode.parse_state_comment(body)
    assert parsed == state


def test_parse_state_comment_ignores_comment_without_marker() -> None:
    assert review_mode.parse_state_comment("그냥 사람이 남긴 코멘트입니다.") is None


def test_render_state_comment_drops_fingerprints_when_too_long() -> None:
    huge_fingerprints = {f"app/file_{i}.py": "f" * 64 for i in range(2000)}
    state = {
        "version": 1,
        "reviewed_head": "a" * 40,
        "reviewed_base": "b" * 40,
        "reviewed_base_tip": "b" * 40,
        "patch_id": "c" * 40,
        "patch_sha256": "d" * 64,
        "file_fingerprints": huge_fingerprints,
        "mode": "full",
        "run_id": "42",
        "reviewed_at": "2026-08-01T00:00:00+00:00",
    }
    body = review_mode.render_state_comment(state)
    assert len(body) <= review_mode.STATE_COMMENT_MAX_CHARS
    parsed = review_mode.parse_state_comment(body)
    assert "file_fingerprints" not in parsed
    # 다음 실행에서 load_state 가 이를 corrupt_state 로 처리해 full 로 fallback 함을 확인.
    _, invalid_reason = review_mode.load_state(body)
    assert invalid_reason == "corrupt_state"


# ── R1 (리뷰 라운드 1) — detect/save-state 가 예외를 내면 리뷰 체크가 빨간불이 된다.
# 어떤 예외에서도(gh api 일시 실패·존재하지 않는 base_ref 등) full 로 fail-safe 하고,
# 그 state 는 신뢰할 수 없으므로(`state_writable=false`) save-state 가 아무것도 쓰지 않아야 한다.
def test_detect_fails_safe_to_full_on_git_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")
    step_summary = tmp_path / "step_summary"
    step_summary.write_text("", encoding="utf-8")

    rc = review_mode.main(
        [
            "detect",
            "--repo-dir",
            str(repo),
            "--workspace",
            str(workspace),
            "--event-action",
            "synchronize",
            "--base-ref",
            "no-such-branch",  # origin/no-such-branch 가 없어 merge-base 가 실패한다
            "--head-sha",
            head1,
            "--pr-number",
            "1",
            "--run-id",
            "1",
            "--github-output",
            str(github_output),
            "--github-step-summary",
            str(step_summary),
            "--state-body-file",
            str(tmp_path / "nonexistent-state.md"),
        ]
    )

    assert rc == 0  # 예외를 내며 죽지 않는다 — job 이 fail 하면 체크가 빨간불이 된다
    output_text = github_output.read_text(encoding="utf-8")
    assert "mode=full" in output_text
    assert "skip_reason=detect_error" in output_text

    meta_path = workspace / ".claude-review" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["mode"] == "full"
    assert meta["state_writable"] is False
    assert meta["skip_reason"].startswith("detect_error")

    comment_out = tmp_path / "comment_out.md"
    rc2 = review_mode.main(
        [
            "save-state",
            "--meta-file",
            str(meta_path),
            "--claude-outcome",
            "success",
            "--comment-out-file",
            str(comment_out),
        ]
    )
    assert rc2 == 0
    assert not comment_out.exists()  # state_writable=false 라 아무것도 쓰지 않는다


# ── F2 (PUBLIC 저장소) — state 코멘트는 github-actions[bot] 작성분만 신뢰한다 ──
# 마커가 든 코멘트를 작성자 확인 없이 신뢰하면, 아무 GitHub 사용자나 위조 코멘트를 올려
# Claude 리뷰를 통째로 skip 시킬 수 있다(공개 저장소이므로 아무나 코멘트를 달 수 있다).
def _marker_comment(comment_id: int, login: str, user_type: str) -> dict:
    return {
        "id": comment_id,
        "body": f"{review_mode.STATE_MARKER}\n```json\n{{}}\n```\n",
        "user": {"login": login, "type": user_type},
    }


def test_filter_trusted_state_comments_separates_bot_from_others() -> None:
    bot_comment = _marker_comment(1, "github-actions[bot]", "Bot")
    human_comment = _marker_comment(2, "some-human", "User")
    no_marker_comment = {"id": 3, "body": "그냥 사람이 남긴 코멘트", "user": {"login": "x", "type": "User"}}

    trusted, untrusted = review_mode.filter_trusted_state_comments(
        [human_comment, bot_comment, no_marker_comment]
    )

    assert trusted == [bot_comment]
    assert untrusted == [human_comment]


def test_filter_trusted_state_comments_ignores_forged_newer_comment() -> None:
    # 위조 시나리오: 진짜 봇 코멘트(오래됨) 뒤에 사람이 마커를 위조한 "더 최신" 코멘트를 올린다.
    # gh api 는 생성 순서(오름차순)로 반환하므로 목록의 "가장 최근"만 보면 위조가 이긴다 —
    # 반드시 작성자로 먼저 걸러야 한다.
    real_bot_comment = _marker_comment(1, "github-actions[bot]", "Bot")
    forged_comment = _marker_comment(2, "attacker", "User")

    trusted, untrusted = review_mode.filter_trusted_state_comments(
        [real_bot_comment, forged_comment]
    )

    assert trusted == [real_bot_comment]  # 최신(forged_comment)이 아니라 신뢰 작성자분만
    assert untrusted == [forged_comment]


def test_gh_fetch_latest_state_comment_returns_none_when_only_human_authored(monkeypatch) -> None:
    human_comment = _marker_comment(1, "some-human", "User")

    class FakeResult:
        stdout = json.dumps([human_comment])

    monkeypatch.setattr(review_mode.subprocess, "run", lambda *a, **kw: FakeResult())

    result = review_mode.gh_fetch_latest_state_comment("owner/repo", 1)

    assert result is None  # → load_state 가 no_state 로 처리해 full 로 fallback


def test_gh_fetch_latest_state_comment_ignores_forged_comment_and_uses_bot_one(
    monkeypatch,
) -> None:
    real_bot_comment = _marker_comment(10, "github-actions[bot]", "Bot")
    forged_comment = _marker_comment(20, "attacker", "User")

    class FakeResult:
        stdout = json.dumps([real_bot_comment, forged_comment])

    monkeypatch.setattr(review_mode.subprocess, "run", lambda *a, **kw: FakeResult())

    result = review_mode.gh_fetch_latest_state_comment("owner/repo", 1)

    assert result is not None
    comment_id, _body = result
    assert comment_id == 10  # forged_comment(20)를 PATCH 하지 않는다 — 애초에 남의 코멘트다


# ── F3 — 따옴표로 인코딩된(한글 등 비-ASCII) 경로는 quotePath=false 로 지문에서 살아남아야 한다 ──
def test_korean_filename_is_not_dropped_from_fingerprints(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    kfile = "app/한글파일.py"
    base0 = _commit(repo, {kfile: "x = 1\n"}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {kfile: "x = 2\n"}, "pr change korean file")

    cur_patch = review_mode.git_diff(repo, base0, head1)
    fp = review_mode.file_fingerprints(cur_patch)
    assert kfile in fp

    state = _full_review_state(repo, base0, head1)
    head2 = _commit(repo, {kfile: "x = 3\n"}, "pr change korean file again")

    ctx = _detect(repo, event_action="synchronize", head_sha=head2, state=state)

    assert ctx.mode != "skip"
    assert ctx.mode == "incremental"


def test_detect_falls_back_to_full_when_fingerprint_coverage_mismatches(
    tmp_path: Path, monkeypatch
) -> None:
    """F3 불변식 검사 — 헤더 파싱이 깨져 file_fingerprints 가 파일을 흘린 것처럼 시뮬레이션한다.
    (quotePath=false 로 실제 원인은 고쳤으므로, 정상 경로로는 더 이상 재현하기 어렵다 —
    불변식 검사 메커니즘 자체가 잡아내는지를 직접 검증한다.)"""
    repo = _init_repo(tmp_path)
    base0 = _commit(repo, {"app/foo.py": FOO_V1}, "init")
    _set_origin_dev(repo, base0)
    _git(repo, "checkout", "-q", "-b", "pr")
    head1 = _commit(repo, {"app/foo.py": FOO_V2}, "pr change")

    monkeypatch.setattr(review_mode, "file_fingerprints", lambda patch_text: {})

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")

    rc = review_mode.main(
        [
            "detect",
            "--repo-dir",
            str(repo),
            "--workspace",
            str(workspace),
            "--event-action",
            "synchronize",
            "--base-ref",
            "dev",
            "--head-sha",
            head1,
            "--pr-number",
            "1",
            "--run-id",
            "1",
            "--github-output",
            str(github_output),
            "--state-body-file",
            str(tmp_path / "nonexistent.md"),
        ]
    )

    assert rc == 0
    output_text = github_output.read_text(encoding="utf-8")
    assert "mode=full" in output_text
    assert "skip_reason=detect_error" in output_text


def test_validate_fingerprint_coverage_raises_on_mismatch() -> None:
    with pytest.raises(RuntimeError):
        review_mode._validate_fingerprint_coverage({"a.py", "b.py"}, {"a.py"})


def test_validate_fingerprint_coverage_ok_when_equal() -> None:
    review_mode._validate_fingerprint_coverage({"a.py"}, {"a.py"})  # raise 없이 통과
