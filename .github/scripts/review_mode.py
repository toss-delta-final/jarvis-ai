#!/usr/bin/env python3
"""Claude PR Review 모드(full/skip/incremental/integration) 판별 및 상태 저장.

이슈 #457 — synchronize 마다 PR 전체를 재리뷰하는 CI 병목을 없애기 위해,
Claude 프롬프트가 아니라 이 스크립트가 git 으로 결정론적으로 모드를 판별한다.
표준 라이브러리만 사용한다(리뷰 워크플로는 `uv sync`를 하지 않는다).

서브커맨드:
- detect: 모드 판별 + budget/prompt 계산 + `.claude-review/` 산출물 기록 + GITHUB_OUTPUT 기록
- save-state: Claude 리뷰 성공 시에만 PR 코멘트의 reviewed state 를 갱신
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── 리뷰 범위 필터 (워크플로의 paths-ignore 와 동일) ──
# CHANGELOG.md 는 거의 모든 PR 이 건드리므로, 걸러내지 않으면 dev 동기화마다
# integration 오탐이 난다. 리뷰 대상이 아닌 파일은 통합 판정 대상도 아니다.
_DOCS_DIR_PREFIX = "docs/"


def in_review_scope(path: str) -> bool:
    """워크플로 `paths-ignore` (`**/*.md`, `docs/**`) 와 동일한 리뷰 범위 필터."""
    if path.endswith(".md"):
        return False
    if path == "docs" or path.startswith(_DOCS_DIR_PREFIX):
        return False
    return True


# ── budget 계층 (이슈 #457 §Claude budget) ──
FULL_BUDGET = 120
INCREMENTAL_BUDGET_TIERS = (40, 60, 100)
INTEGRATION_BUDGET_TIERS = (60, 80, 100)
BUDGET_ESCALATE_LINES = 150
BUDGET_ESCALATE_FILES = 3
BUDGET_MAX_LINES = 500
BUDGET_MAX_FILES = 8

# 고영향 경로 — 계약 표면(api/schemas/api-spec)·공용 설정(core)·핵심 파이프라인(pipelines)·
# CI 자체는 작은 변경도 영향 범위가 넓다(이슈: "작은 코드 변경이라도 공용 interface 또는
# 핵심 pipeline 동작을 바꾸면 높은 budget"). CI 스크립트 전용 상수 — app/core/config.py 를
# import 하지 않는다(리뷰 워크플로는 uv sync 를 하지 않는다).
HIGH_IMPACT_PATH_PREFIXES = (
    "app/api/",
    "app/schemas/",
    "app/core/",
    "app/pipelines/",
    ".github/workflows/",
)
HIGH_IMPACT_EXACT_PATHS = ("docs/api-spec.md",)


def _is_high_impact_path(path: str) -> bool:
    if path in HIGH_IMPACT_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in HIGH_IMPACT_PATH_PREFIXES)


def compute_budget(mode: str, changed_line_count: int, target_file_count: int, target_files) -> int:
    """incremental/integration 의 max-turns budget 을 3단 계층으로 산출한다."""
    if mode == "incremental":
        base, escalated, maxed = INCREMENTAL_BUDGET_TIERS
    elif mode == "integration":
        base, escalated, maxed = INTEGRATION_BUDGET_TIERS
    else:
        raise ValueError(f"compute_budget 은 incremental/integration 전용: mode={mode!r}")

    if (
        changed_line_count > BUDGET_MAX_LINES
        or target_file_count > BUDGET_MAX_FILES
        or any(_is_high_impact_path(p) for p in target_files)
    ):
        return maxed
    if changed_line_count > BUDGET_ESCALATE_LINES or target_file_count > BUDGET_ESCALATE_FILES:
        return escalated
    return base


def count_changed_lines(diff_text: str) -> int:
    """diff 의 +/- 변경 줄 수 (파일 헤더 `+++`/`---` 는 제외)."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


# ── patch 지문 (§2.2) ──
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.*) b/(?P<b>.*)$")
# `index <blob-a>..<blob-b> <mode>` 줄의 blob SHA 는 **텍스트 diff** 에서는 파일의 다른 부분이
# 바뀌기만 해도 같은 hunk 를 "바뀜"으로 오탐시키는 잡음이라 정규화한다(리뷰 라운드 1 R2 실측:
# 20줄 떨어진 다른 함수만 바뀐 merge 에서도 index 줄 때문에 지문이 달라졌다). 단 **바이너리
# diff** 는 index 줄의 blob SHA 가 유일한 내용 신호라 정규화하면 안 된다(F4 실측: 서로 다른
# 바이너리 내용의 index 줄만 다른 두 diff 가 정규화 후 같은 지문이 돼 skip 으로 샜다) —
# `_normalize_for_fingerprint` 가 바이너리 조각을 감지해 건너뛴다.
_INDEX_LINE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: \d+)?$", re.MULTILINE)
_BINARY_DIFF_MARKERS = ("Binary files ", "GIT binary patch")


def split_patch_by_file(patch_text: str) -> dict[str, str]:
    """`git diff` 출력을 `diff --git` 헤더 기준으로 파일별 조각으로 나눈다.

    키는 b/ 경로(변경 후 경로) — rename 도 새 경로 기준, delete 도 git 이
    diff --git 줄에서 a/·b/ 모두 원본 경로를 쓰므로 일관된다.
    """
    if not patch_text:
        return {}
    chunks: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for line in patch_text.splitlines(keepends=True):
        m = _DIFF_GIT_HEADER_RE.match(line)
        if m:
            if current_path is not None:
                chunks[current_path] = current_lines
            current_path = m.group("b")
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        chunks[current_path] = current_lines
    return {path: "".join(lines) for path, lines in chunks.items()}


def _is_binary_diff_chunk(file_patch_text: str) -> bool:
    return any(marker in file_patch_text for marker in _BINARY_DIFF_MARKERS)


def _normalize_for_fingerprint(file_patch_text: str) -> str:
    """지문 계산 전 파일 조각을 정규화한다. 순수 함수.

    hunk 헤더(`@@ -a,b +c,d @@`)의 줄 번호는 **더 이상 지우지 않는다**(F5) — "base 이동으로
    hunk 위치만 밀리는" 상황은 그 파일을 dev 가 건드렸다는 뜻이라 항상 overlap → integration 으로
    먼저 잡힌다. 즉 이 정규화는 실제 모드 판정을 한 번도 바꾸지 못하면서, "같은 변경 내용이
    의미가 다른 위치로 옮겨진 진짜 변경"(반복되는 설정/테이블에서 앞뒤 3줄 컨텍스트가 우연히
    같은 경우)을 "변경 없음"으로 오판시킬 위험만 남겼다 — 리뷰를 한 번 더 도는 쪽이 안전하다.

    `index <blob>..<blob>` 의 blob SHA 는 **텍스트 diff 에서만** 정규화한다(F4) — 바이너리
    diff 는 index 줄이 유일한 내용 신호라 지우면 서로 다른 바이너리 변경이 뭉개진다.
    """
    if _is_binary_diff_chunk(file_patch_text):
        return file_patch_text
    return _INDEX_LINE_RE.sub("index <blob>", file_patch_text)


def file_fingerprints(patch_text: str) -> dict[str, str]:
    """파일별 지문: 정규화한 patch 조각의 sha256. 순수 함수.

    내용(공백·들여쓰기·hunk 위치 포함)은 그대로 반영한다 — `git patch-id --verbatim` 과
    같은 이유로, 공백만 바뀐 변경을 "동일"로 오판하면 안 된다(§2.3 시나리오 7).
    """
    chunks = split_patch_by_file(patch_text)
    return {
        path: hashlib.sha256(_normalize_for_fingerprint(chunk).encode("utf-8")).hexdigest()
        for path, chunk in chunks.items()
    }


# ── state 코멘트 직렬화 (§2.1) ──
STATE_MARKER = "<!-- claude-review-state:v1 -->"
STATE_COMMENT_MAX_CHARS = 60_000  # GitHub 코멘트 상한 65536 자 아래 안전 마진
_STATE_JSON_BLOCK_RE = re.compile(r"```json\n(?P<json>.*?)\n```", re.DOTALL)


def render_state_comment(state: dict) -> str:
    """state dict → PR 코멘트 본문. 직렬화가 60000자를 넘으면 file_fingerprints 를 생략한다."""
    body = json.dumps(state, ensure_ascii=False, sort_keys=True)
    text = _render_state_comment_text(body)
    if len(text) > STATE_COMMENT_MAX_CHARS and "file_fingerprints" in state:
        trimmed = {k: v for k, v in state.items() if k != "file_fingerprints"}
        body = json.dumps(trimmed, ensure_ascii=False, sort_keys=True)
        text = _render_state_comment_text(body)
    return text


def _render_state_comment_text(json_body: str) -> str:
    return (
        f"{STATE_MARKER}\n"
        "<details><summary>Claude Review state (자동 생성 — 수정하지 마세요)</summary>\n\n"
        f"```json\n{json_body}\n```\n\n"
        "</details>"
    )


def parse_state_comment(body: str | None) -> dict | None:
    """PR 코멘트 본문 → state dict. 마커/json 블록이 없거나 파싱 실패하면 None. 순수 함수."""
    if not body or STATE_MARKER not in body:
        return None
    match = _STATE_JSON_BLOCK_RE.search(body)
    if not match:
        return None
    try:
        parsed = json.loads(match.group("json"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_state(comment_body: str | None) -> tuple[dict | None, str | None]:
    """comment_body → (state, invalid_reason). invalid_reason 은
    no_state(코멘트 없음/마커 없음) | corrupt_state(파싱 실패·버전 불일치·필수 키 없음) | None(유효).
    순수 함수 — git 저장소 존재 확인(§2.3 stale_state)은 호출부에서 별도 수행한다.
    """
    if comment_body is None or STATE_MARKER not in comment_body:
        return None, "no_state"
    parsed = parse_state_comment(comment_body)
    if parsed is None:
        return None, "corrupt_state"
    if parsed.get("version") != 1:
        return None, "corrupt_state"
    if "file_fingerprints" not in parsed:
        return None, "corrupt_state"
    if "reviewed_head" not in parsed or "reviewed_base" not in parsed:
        return None, "corrupt_state"
    if "reviewed_base_tip" not in parsed:  # F1 — 통합 판정은 base tip 기준
        return None, "corrupt_state"
    return parsed, None


# ── 모드 판별 (§2.3) ──
@dataclass(frozen=True)
class ModeDecision:
    mode: str
    skip_reason: str
    changed_files: frozenset[str]
    base_files: frozenset[str]
    overlap: frozenset[str]


def decide_mode(
    *,
    event_action: str,
    state_invalid_reason: str | None,
    reviewed_base_tip: str | None,
    cur_base_tip: str,
    prev_fp: dict[str, str],
    cur_fp: dict[str, str],
    base_files: frozenset[str],
) -> ModeDecision:
    """모드 판별 알고리즘 (이슈 #457 §2.3, 이 순서 그대로).

    `base_changed`/`overlap` 판정은 **merge-base 가 아니라 base 브랜치 tip** 으로 한다(F1) —
    merge-base(origin/<base>, head) 는 PR 이 dev 를 머지하지 않는 한 dev 가 아무리 전진해도
    그대로라, "dev 가 PR 파일을 바꿨지만 PR 은 아직 안 받은" 통합 변화(이슈 §4 의 "conflict 는
    없지만 base 변경으로 PR 코드의 의미가 달라짐")를 skip 으로 놓친다. PR 고유 기여분(patch)
    계산은 여전히 merge-base 를 쓴다(그게 PR 이 실제로 만든 diff 다) — 이 함수는 그 값을
    받지 않는다.

    `base_files` 는 이미 리뷰 범위로 필터링된 `git diff --name-only <reviewed_base_tip>
    <cur_base_tip>` (base_changed 가 아니면 빈 집합을 넘겨도 무방 — 사용되지 않는다).
    """
    if event_action == "opened":
        return ModeDecision("full", "opened", frozenset(), frozenset(), frozenset())

    if state_invalid_reason is not None:
        return ModeDecision("full", state_invalid_reason, frozenset(), frozenset(), frozenset())

    all_keys = frozenset(prev_fp.keys()) | frozenset(cur_fp.keys())
    pr_files = frozenset(k for k in all_keys if in_review_scope(k))
    changed_files = frozenset(
        k for k in pr_files if prev_fp.get(k) != cur_fp.get(k)
    )
    base_changed = reviewed_base_tip != cur_base_tip
    overlap = (frozenset(base_files) & pr_files) if base_changed else frozenset()

    if base_changed and overlap:
        return ModeDecision("integration", "base_overlap", changed_files, base_files, overlap)
    if not changed_files:
        return ModeDecision("skip", "patch_identical", changed_files, base_files, overlap)
    return ModeDecision("incremental", "patch_changed", changed_files, base_files, overlap)


# ── 프롬프트 (§2.6, 이슈 §Prompt 원칙 문구 승계) ──
# "중점" 목록은 모드별 본문(이슈 §Prompt 원칙 문구)에만 둔다 — 여기서는 전달 방식만 다룬다
# (리뷰 라운드 1 R7: 공통 규칙에도 "중점" 한 줄이 있어 모드 본문과 지시가 중복됐었다).
_COMMON_PROMPT_RULES = (
    "인라인 코멘트는 반드시 `mcp__github_inline_comment__create_inline_comment` 도구를 "
    "`confirmed: true` 로 호출해 게시해.\n"
    "사소한 포매팅·네이밍은 지적하지 마. `docs/`·`*.md` 문서는 무시. 한국어로."
)

_FULL_PROMPT_BODY = (
    "이 PR 전체 변경을 리뷰한다.\n\n"
    "변경 코드 검증에 필요한 경우 관련 기존 코드, caller/callee,\n"
    "config, schema/model, api-spec, test를 확인한다.\n\n"
    "중점:\n"
    "- 논리 버그\n"
    "- 보안\n"
    "- IDOR / 신원 및 권한 처리\n"
    "- api-spec 계약 위반\n"
    "- 미검증 입력\n"
    "- 실제 회귀\n"
    "- 의미 있는 성능 문제\n\n"
    "포매팅, 네이밍, 사소한 스타일은 지적하지 않는다."
)

_INCREMENTAL_PROMPT_BODY = (
    "Primary review target은 마지막 성공 Claude Review 이후 변경된 PR patch다.\n\n"
    "기존 PR 전체를 처음부터 다시 리뷰하지 않는다.\n\n"
    "다만 이번 수정의 정확성을 검증하기 위해 필요한 caller/callee,\n"
    "config, model/schema, contract, test 등 기존 코드는 Read/Grep으로 확인할 수 있다.\n\n"
    "이번 변경으로 새로 발생하거나 노출된 버그, 회귀, 보안 문제,\n"
    "계약 위반만 보고한다.\n\n"
    "primary review target 은 `.claude-review/target.diff` 파일이다 — `Read` 로 읽어라."
)

_INTEGRATION_PROMPT_BODY = (
    "이번 변경에는 base branch(dev) 동기화 또는 통합 영향이 포함되어 있다.\n\n"
    "마지막 성공 리뷰 이후 달라진 PR patch를 중심으로 리뷰한다.\n\n"
    "특히:\n"
    "- conflict resolution 결과\n"
    "- PR과 dev에서 동시에 변경된 코드\n"
    "- base 변경으로 의미가 달라진 caller/callee\n"
    "- interface / schema / config / contract 변화\n"
    "- 관련 테스트\n\n"
    "를 확인한다.\n\n"
    "dev 전체를 새로 리뷰하지 않고 이번 PR과 연결되는 base 변경만 확인한다.\n\n"
    "primary review target 은 `.claude-review/target.diff` 파일이다 — `Read` 로 읽어라.\n"
    "`.claude-review/base-context.diff` (dev 쪽 변경)도 읽고, PR 과 dev 에서 동시에 수정된\n"
    "코드의 최종 통합 결과가 올바른지 집중 검증해라."
)

_PROMPT_BODIES = {
    "full": _FULL_PROMPT_BODY,
    "incremental": _INCREMENTAL_PROMPT_BODY,
    "integration": _INTEGRATION_PROMPT_BODY,
}

# F1 — base tip 기준으로 판별하면 PR 쪽 변경분(target.diff)이 0인 integration 이 나올 수
# 있다(dev 만 PR 파일을 움직인 경우). 그 상태로 프롬프트가 나가면 리뷰어가 빈 target.diff 를
# 보고 헤맨다 — base-context.diff 로 시선을 돌리는 문장을 동적으로 덧붙인다.
_INTEGRATION_EMPTY_TARGET_NOTE = (
    "마지막 리뷰 이후 PR 쪽 변경은 없고, 이번 리뷰 대상은 base(dev) 변경이 PR 코드와 만나는 "
    "지점이다. `.claude-review/base-context.diff` 를 중심으로 봐라."
)


def build_prompt(mode: str, *, target_diff_empty: bool = False) -> str:
    """모드별 Claude 프롬프트. 순수 함수 — 이슈 §Prompt 원칙 한국어 문구를 그대로 승계."""
    body = _PROMPT_BODIES[mode]
    prompt = f"{body}\n\n{_COMMON_PROMPT_RULES}"
    if mode == "integration" and target_diff_empty:
        prompt += f"\n\n{_INTEGRATION_EMPTY_TARGET_NOTE}"
    return prompt


# ── Claude 리뷰 성공 판정 (§2.7) ──
def is_review_successful(claude_outcome: str, execution_file_content: str | None) -> tuple[bool, str]:
    """anthropics/claude-code-action@v1 은 conclusion 출력이 없어 execution_file 을 직접 판정한다.
    순수 함수 — execution_file 은 이미 읽은 텍스트(또는 없으면 None)를 받는다."""
    if claude_outcome != "success":
        return False, f"claude_step_outcome={claude_outcome}"
    if not execution_file_content:
        return False, "execution_file_missing_or_empty"
    try:
        messages = json.loads(execution_file_content)
    except json.JSONDecodeError as exc:
        return False, f"execution_file_parse_error:{exc}"
    if not isinstance(messages, list) or not messages:
        return False, "execution_file_not_nonempty_list"
    # 마지막 원소만 보면 result 뒤에 메시지가 하나라도 더 붙을 때 성공한 리뷰가 매번
    # "미완료"로 오판된다 — 뒤에서부터 type=="result" 인 마지막 메시지를 찾는다(R3).
    result_message = next(
        (m for m in reversed(messages) if isinstance(m, dict) and m.get("type") == "result"),
        None,
    )
    if result_message is None:
        return False, "no_result_message"
    if result_message.get("subtype") != "success":
        return False, f"result_subtype={result_message.get('subtype')}"
    if result_message.get("is_error"):
        return False, "result_is_error_true"
    return True, "ok"


# ── git I/O ──
def _run_git(repo_dir: Path, *args: str, input_text: str | None = None) -> str:
    # -c core.quotePath=false — 기본값(true)은 비-ASCII(한글 등) 경로를 8진 이스케이프로
    # 따옴표 인코딩해 `diff --git a/"..." b/"..."` 형태로 내고, 그러면 _DIFF_GIT_HEADER_RE 가
    # 그 파일을 통째로 놓친다(F3 실측 — 놓친 파일은 지문에서 사라져 "변경 없음"으로 오판된다).
    # encoding/errors="replace" — diff 안에 UTF-8 이 아닌 바이트가 섞여도 죽지 않는다(R4).
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=repo_dir,
        input=input_text,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def git_merge_base(repo_dir: Path, base_ref: str, head_sha: str) -> str:
    return _run_git(repo_dir, "merge-base", f"origin/{base_ref}", head_sha).strip()


def git_rev_parse(repo_dir: Path, ref: str) -> str:
    return _run_git(repo_dir, "rev-parse", ref).strip()


def git_diff(repo_dir: Path, rev_a: str, rev_b: str, paths: list[str] | None = None) -> str:
    args = ["diff", rev_a, rev_b]
    if paths:
        args.append("--")
        args.extend(paths)
    return _run_git(repo_dir, *args)


def git_diff_name_only(repo_dir: Path, rev_a: str, rev_b: str) -> list[str]:
    out = _run_git(repo_dir, "diff", "--name-only", rev_a, rev_b)
    return [line for line in out.splitlines() if line]


def git_diff_name_only_z(repo_dir: Path, rev_a: str, rev_b: str) -> list[str]:
    """`-z` 로 NUL 구분 목록을 받는다 — quotePath 설정과 무관하게 항상 원문 경로다.
    `file_fingerprints` 가 실제로 어떤 파일을 흘렸는지 대조하는 권위 있는 목록(F3)."""
    out = _run_git(repo_dir, "diff", "--name-only", "-z", rev_a, rev_b)
    return [p for p in out.split("\0") if p]


def _validate_fingerprint_coverage(authoritative_files: set[str], fingerprinted_files: set[str]) -> None:
    """`file_fingerprints` 의 키 집합이 권위 있는 파일 목록과 다르면 예외를 낸다(F3 불변식 검사).

    조용히 파일을 흘리는 것(그 파일이 "변경 없음"으로 오판돼 리뷰 없이 통과)보다, 예외를 던져
    `cmd_detect` 의 R1 fail-safe 로 full 리뷰를 한 번 더 도는 게 안전하다.
    """
    if authoritative_files == fingerprinted_files:
        return
    missing = sorted(authoritative_files - fingerprinted_files)
    extra = sorted(fingerprinted_files - authoritative_files)
    raise RuntimeError(
        "file_fingerprints 파일 목록이 git diff --name-only 권위 목록과 다르다: "
        f"missing={missing} extra={extra}"
    )


def git_patch_id(repo_dir: Path, patch_text: str) -> str:
    if not patch_text.strip():
        return ""
    out = _run_git(repo_dir, "patch-id", "--verbatim", input_text=patch_text)
    first_line = out.strip().splitlines()[0] if out.strip() else ""
    return first_line.split()[0] if first_line else ""


def git_commit_exists(repo_dir: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", sha],
        cwd=repo_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


# ── detect 오케스트레이션 ──
@dataclass
class DetectContext:
    mode: str
    skip_reason: str
    budget: int | None
    cur_base: str
    cur_base_tip: str
    cur_head: str
    cur_fp: dict[str, str]
    patch_id: str
    patch_sha256: str
    target_files: frozenset[str]
    target_diff: str
    base_context_diff: str
    prompt: str
    prev_base: str | None
    prev_head: str | None
    prev_base_tip: str | None = None
    base_files: frozenset[str] = field(default_factory=frozenset)
    changed_files: frozenset[str] = field(default_factory=frozenset)
    overlap: frozenset[str] = field(default_factory=frozenset)


def detect_review_context(
    repo_dir: Path,
    *,
    event_action: str,
    base_ref: str,
    head_sha: str,
    state_comment_body: str | None,
) -> DetectContext:
    """모드 판별 + budget/target diff/prompt 계산. git 을 실제로 호출한다(순수 함수 아님)."""
    # PR 고유 기여분(patch) 계산은 merge-base 를 쓴다 — PR 이 실제로 만든 diff 다.
    cur_base = git_merge_base(repo_dir, base_ref, head_sha)
    # 통합(integration) 판정은 base 브랜치 **tip** 을 쓴다(F1) — merge-base 는 PR 이 dev 를
    # 머지하지 않는 한 dev 가 전진해도 그대로라, 아직 안 받은 통합 변화를 skip 으로 놓친다.
    cur_base_tip = git_rev_parse(repo_dir, f"origin/{base_ref}")
    cur_patch = git_diff(repo_dir, cur_base, head_sha)
    cur_fp = file_fingerprints(cur_patch)

    # F3 불변식 검사 — split_patch_by_file 이 조용히 파일을 흘리지 않았는지 권위 있는 목록과
    # 대조한다. 어긋나면 예외를 던져 cmd_detect 의 R1 fail-safe(full)로 떨어진다.
    authoritative_files = set(git_diff_name_only_z(repo_dir, cur_base, head_sha))
    _validate_fingerprint_coverage(authoritative_files, set(cur_fp.keys()))

    patch_id = git_patch_id(repo_dir, cur_patch)
    patch_sha256 = hashlib.sha256(cur_patch.encode("utf-8")).hexdigest()

    state, invalid_reason = load_state(state_comment_body)
    if state is not None:
        if (
            not git_commit_exists(repo_dir, state["reviewed_head"])
            or not git_commit_exists(repo_dir, state["reviewed_base"])
            or not git_commit_exists(repo_dir, state["reviewed_base_tip"])
        ):
            state, invalid_reason = None, "stale_state"

    reviewed_base = state["reviewed_base"] if state else None
    reviewed_base_tip = state["reviewed_base_tip"] if state else None
    reviewed_head = state["reviewed_head"] if state else None
    prev_fp = state["file_fingerprints"] if state else {}

    base_files: frozenset[str] = frozenset()
    if state is not None and reviewed_base_tip != cur_base_tip:
        assert reviewed_base_tip is not None  # state is not None => reviewed_base_tip 은 str
        raw_base_files = git_diff_name_only(repo_dir, reviewed_base_tip, cur_base_tip)
        base_files = frozenset(p for p in raw_base_files if in_review_scope(p))

    decision = decide_mode(
        event_action=event_action,
        state_invalid_reason=invalid_reason,
        reviewed_base_tip=reviewed_base_tip,
        cur_base_tip=cur_base_tip,
        prev_fp=prev_fp,
        cur_fp=cur_fp,
        base_files=base_files,
    )

    target_files: frozenset[str] = frozenset()
    target_diff = ""
    base_context_diff = ""
    budget: int | None = None

    if decision.mode == "full":
        budget = FULL_BUDGET
    elif decision.mode in ("incremental", "integration") and reviewed_head is not None:
        target_files = decision.changed_files | decision.overlap
        target_diff = git_diff(repo_dir, reviewed_head, head_sha, sorted(target_files))
        changed_line_count = count_changed_lines(target_diff)
        if decision.mode == "integration" and decision.overlap:
            assert reviewed_base_tip is not None  # integration 은 base_changed 전제
            base_context_diff = git_diff(
                repo_dir, reviewed_base_tip, cur_base_tip, sorted(decision.overlap)
            )
            # F1 파생효과 2 — integration budget 입력은 target.diff 만이 아니라
            # base-context.diff 도 합친다. PR 쪽 변경이 0(dev 만 움직인 경우)이면 target.diff
            # 만으로는 항상 최저 단계(60)가 된다.
            changed_line_count += count_changed_lines(base_context_diff)
        budget = compute_budget(decision.mode, changed_line_count, len(target_files), target_files)

    if decision.mode != "skip" and budget is None:
        # 구조적으로 도달 불가(incremental/integration 은 항상 reviewed_head 를 전제)하지만,
        # `--max-turns` 에 빈 값이 나가 액션 인자 오류로 빨간불이 되는 것을 막는 안전망(R5).
        print(
            f"::warning::budget 산출 실패(mode={decision.mode}) — FULL_BUDGET 으로 안전하게 대체한다."
        )
        budget = FULL_BUDGET

    # F1 파생효과 1 — dev 만 PR 파일을 움직인 integration 은 target.diff 가 빌 수 있다.
    # 그 상태로 나가면 리뷰어가 헤매니, base-context.diff 로 시선을 돌리는 문장을 덧붙인다.
    target_diff_empty = decision.mode == "integration" and not target_diff.strip()
    prompt = (
        ""
        if decision.mode == "skip"
        else build_prompt(decision.mode, target_diff_empty=target_diff_empty)
    )

    return DetectContext(
        mode=decision.mode,
        skip_reason=decision.skip_reason,
        budget=budget,
        cur_base=cur_base,
        cur_base_tip=cur_base_tip,
        cur_head=head_sha,
        cur_fp=cur_fp,
        patch_id=patch_id,
        patch_sha256=patch_sha256,
        target_files=target_files,
        target_diff=target_diff,
        base_context_diff=base_context_diff,
        prompt=prompt,
        prev_base=reviewed_base,
        prev_base_tip=reviewed_base_tip,
        prev_head=reviewed_head,
        base_files=decision.base_files,
        changed_files=decision.changed_files,
        overlap=decision.overlap,
    )


# ── gh I/O ──
# 이 저장소는 PUBLIC 이다(F2) — 아무 GitHub 사용자나 PR 에 마커가 든 코멘트를 위조해 올려
# Claude 리뷰를 통째로 skip 시킬 수 있다. state 코멘트는 우리 워크플로가 쓰는 봇 작성분만 신뢰한다.
STATE_COMMENT_TRUSTED_LOGIN = "github-actions[bot]"


def is_trusted_state_author(user: dict) -> bool:
    """state 코멘트 작성자가 신뢰할 수 있는 봇인지. 순수 함수."""
    return user.get("login") == STATE_COMMENT_TRUSTED_LOGIN and user.get("type") == "Bot"


def filter_trusted_state_comments(comments: list[dict]) -> tuple[list[dict], list[dict]]:
    """마커가 든 코멘트를 (신뢰 작성자분, 그 외) 로 나눈다. 순수 함수 — `gh api` 응답 JSON
    리스트를 그대로 받아 유닛 테스트로 검증할 수 있게 gh 호출과 분리했다."""
    trusted: list[dict] = []
    untrusted: list[dict] = []
    for comment in comments:
        if STATE_MARKER not in comment.get("body", ""):
            continue
        if is_trusted_state_author(comment.get("user") or {}):
            trusted.append(comment)
        else:
            untrusted.append(comment)
    return trusted, untrusted


def gh_fetch_latest_state_comment(repo: str, pr_number: int) -> tuple[int, str] | None:
    result = subprocess.run(
        ["gh", "api", f"/repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    comments = json.loads(result.stdout)
    trusted, untrusted = filter_trusted_state_comments(comments)
    if untrusted:
        print(
            f"::warning::신뢰할 수 없는 작성자의 state 코멘트 {len(untrusted)}건을 무시했다 "
            f"({STATE_COMMENT_TRUSTED_LOGIN} 만 신뢰) — 위조 시도이거나 사람이 실수로 마커를 "
            "복사한 코멘트일 수 있다."
        )
    if not trusted:
        return None
    latest = trusted[-1]
    return latest["id"], latest["body"]


def gh_create_comment(repo: str, pr_number: int, body: str) -> None:
    subprocess.run(
        ["gh", "api", f"/repos/{repo}/issues/{pr_number}/comments", "-f", f"body={body}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def gh_patch_comment(repo: str, comment_id: int, body: str) -> None:
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


# ── GITHUB_OUTPUT / STEP_SUMMARY 기록 ──
def _multiline_output(key: str, value: str) -> str:
    delimiter = f"ghadelim_{secrets.token_hex(8)}"
    while delimiter in value:
        delimiter = f"ghadelim_{secrets.token_hex(8)}"
    return f"{key}<<{delimiter}\n{value}\n{delimiter}\n"


def write_github_output(path: str, outputs: dict[str, str], multiline_keys: set[str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for key, value in outputs.items():
            if key in multiline_keys:
                f.write(_multiline_output(key, value))
            else:
                f.write(f"{key}={value}\n")


_OBSERVATION_KEYS = (
    "review_mode",
    "previous_reviewed_head",
    "current_head",
    "previous_base",
    "current_base",
    "previous_base_tip",
    "current_base_tip",
    "patch_changed",
    "base_changed",
    "review_budget",
    "changed_files_since_review",
    "integration_related_files",
    "target_files",
    "skip_reason",
)


def build_observation(ctx: DetectContext) -> dict[str, str]:
    # patch_changed/changed_files_since_review/integration_related_files 는 mode 에서 역산하지
    # 않고 decide_mode 의 실제 판정 결과(changed_files/overlap)를 그대로 옮긴다(리뷰 라운드 1 R2) —
    # integration 은 patch 가 동일해도(overlap 우선 판정) 발생할 수 있어, mode 로부터 역산하면
    # 그 케이스에서 patch_changed 가 거짓말을 한다. full/opened/state 불량이라 비교할 이전 state
    # 자체가 없을 때는 false 가 아니라 unknown 이다.
    patch_changed = "unknown" if ctx.mode == "full" else ("true" if ctx.changed_files else "false")
    # base_changed 는 merge-base(prev_base/current_base) 가 아니라 base tip 으로 판정한다(F1) —
    # 통합 판정 자체가 tip 기준이라, 여기서 merge-base 를 쓰면 mode=integration 인데
    # base_changed=false 라고 로그가 스스로 모순되는 R2 와 같은 종류의 거짓말을 한다.
    base_changed = "true" if (ctx.prev_base_tip is not None and ctx.prev_base_tip != ctx.cur_base_tip) else "false"
    return {
        "review_mode": ctx.mode,
        "previous_reviewed_head": ctx.prev_head or "",
        "current_head": ctx.cur_head,
        "previous_base": ctx.prev_base or "",
        "current_base": ctx.cur_base,
        "previous_base_tip": ctx.prev_base_tip or "",
        "current_base_tip": ctx.cur_base_tip,
        "patch_changed": patch_changed,
        "base_changed": base_changed,
        "review_budget": str(ctx.budget) if ctx.budget is not None else "",
        "changed_files_since_review": ",".join(sorted(ctx.changed_files)) or "(none)",
        "integration_related_files": ",".join(sorted(ctx.overlap)) or "(none)",
        "target_files": ",".join(sorted(ctx.target_files)) or "(none)",
        "skip_reason": ctx.skip_reason,
    }


def write_observation_log(observation: dict[str, str]) -> None:
    print("::group::review_mode detect")
    for key in _OBSERVATION_KEYS:
        print(f"{key}={observation[key]}")
    print("::endgroup::")


def write_step_summary(path: str, observation: dict[str, str]) -> None:
    lines = ["## Claude Review mode 판별\n", "| key | value |", "| --- | --- |"]
    for key in _OBSERVATION_KEYS:
        lines.append(f"| `{key}` | `{observation[key]}` |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _detect_error_reason(exc: BaseException) -> str:
    """예외 → skip_reason 문자열(짧게 자른다). state_writable=False 로 fallback 할 때 쓴다(R1)."""
    detail = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return f"detect_error:{detail[:200]}"


def _write_detect_fallback(args: argparse.Namespace, exc: BaseException) -> int:
    """detect 본체가 어떤 이유로든 예외를 내면 여기로 떨어진다.

    이슈 §"실패 시 fallback"(state 불확실 → Full Review, 성능보다 false skip 회피)을 그대로 따라
    **항상 full 로 내려앉는다** — 일시적 gh/git 오류로 review 체크가 빨간불이 되면 안 된다(R1).
    이 경로에서 나온 meta.json 은 `state_writable: false` 라 save-state 가 절대 쓰지 않는다 —
    다음 실행이 다시 full 이 되는 안전한 방향이다.
    """
    skip_reason = _detect_error_reason(exc)
    print(f"::warning::detect 스텝 실패 — full 로 fail-safe: {skip_reason}")

    observation = {
        "review_mode": "full",
        "previous_reviewed_head": "",
        "current_head": args.head_sha,
        "previous_base": "",
        "current_base": "",
        "previous_base_tip": "",
        "current_base_tip": "",
        "patch_changed": "unknown",
        "base_changed": "unknown",
        "review_budget": str(FULL_BUDGET),
        "changed_files_since_review": "(unknown)",
        "integration_related_files": "(unknown)",
        "target_files": "(unknown)",
        "skip_reason": skip_reason,
    }
    write_observation_log(observation)
    if args.github_step_summary:
        write_step_summary(args.github_step_summary, observation)
    if args.github_output:
        outputs = {
            "mode": "full",
            "budget": str(FULL_BUDGET),
            "prompt": build_prompt("full"),
            "skip_reason": skip_reason,
        }
        write_github_output(args.github_output, outputs, multiline_keys={"prompt"})

    try:
        review_dir = Path(args.workspace) / ".claude-review"
        review_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "mode": "full",
            "skip_reason": skip_reason,
            "budget": FULL_BUDGET,
            "state_writable": False,
            "pr_number": args.pr_number,
            "run_id": args.run_id,
        }
        (review_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (review_dir / "target.diff").write_text("", encoding="utf-8")
    except OSError as meta_exc:
        # meta.json 조차 못 쓰면 save-state 가 파일 없음으로 이미 no-op 한다 — 로그만 남긴다.
        print(f"::warning::detect fallback meta.json 기록 실패: {meta_exc!r}")

    return 0


# ── CLI: detect ──
def cmd_detect(args: argparse.Namespace) -> int:
    try:
        return _cmd_detect_impl(args)
    except Exception as exc:  # noqa: BLE001 - 어떤 예외에서도 리뷰를 포기하지 않는다(R1)
        return _write_detect_fallback(args, exc)


def _cmd_detect_impl(args: argparse.Namespace) -> int:
    repo_dir = Path(args.repo_dir)
    workspace = Path(args.workspace)
    review_dir = workspace / ".claude-review"
    review_dir.mkdir(parents=True, exist_ok=True)

    existing_comment_id: int | None = None
    if args.state_body_file:
        state_body_path = Path(args.state_body_file)
        state_body = state_body_path.read_text(encoding="utf-8") if state_body_path.exists() else None
    else:
        fetched = gh_fetch_latest_state_comment(args.repo, args.pr_number)
        if fetched is not None:
            existing_comment_id, state_body = fetched
        else:
            state_body = None

    ctx = detect_review_context(
        repo_dir,
        event_action=args.event_action,
        base_ref=args.base_ref,
        head_sha=args.head_sha,
        state_comment_body=state_body,
    )

    (review_dir / "target.diff").write_text(ctx.target_diff, encoding="utf-8")
    if ctx.mode == "integration":
        (review_dir / "base-context.diff").write_text(ctx.base_context_diff, encoding="utf-8")

    meta = {
        "mode": ctx.mode,
        "skip_reason": ctx.skip_reason,
        "budget": ctx.budget,
        "state_writable": True,
        "cur_base": ctx.cur_base,
        "cur_base_tip": ctx.cur_base_tip,
        "cur_head": ctx.cur_head,
        "cur_fp": ctx.cur_fp,
        "patch_id": ctx.patch_id,
        "patch_sha256": ctx.patch_sha256,
        "target_files": sorted(ctx.target_files),
        "existing_state_comment_id": existing_comment_id,
        "pr_number": args.pr_number,
        "run_id": args.run_id,
    }
    (review_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    observation = build_observation(ctx)
    write_observation_log(observation)
    if args.github_step_summary:
        write_step_summary(args.github_step_summary, observation)

    if args.github_output:
        outputs = {
            "mode": ctx.mode,
            "budget": str(ctx.budget) if ctx.budget is not None else "",
            "prompt": ctx.prompt,
            "skip_reason": ctx.skip_reason,
        }
        write_github_output(args.github_output, outputs, multiline_keys={"prompt"})

    return 0


# ── CLI: save-state ──
_SAVE_STATE_REQUIRED_META_KEYS = (
    "cur_head",
    "cur_base",
    "cur_base_tip",
    "cur_fp",
    "patch_id",
    "patch_sha256",
    "pr_number",
    "run_id",
)


def cmd_save_state(args: argparse.Namespace) -> int:
    try:
        return _cmd_save_state_impl(args)
    except Exception as exc:  # noqa: BLE001 - 리뷰는 이미 성공했는데 체크만 빨개지면 안 된다(R1)
        print(f"::warning::save-state 실패 — state 를 갱신하지 않는다: {exc!r}")
        return 0


def _cmd_save_state_impl(args: argparse.Namespace) -> int:
    try:
        meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::meta.json 읽기/파싱 실패 — state 를 갱신하지 않는다: {exc!r}")
        return 0

    mode = meta.get("mode")
    if mode == "skip":
        print("skip 모드 — patch 가 그대로라 기존 state 가 여전히 정확하다. 갱신하지 않는다.")
        return 0

    if meta.get("state_writable") is False:
        print(
            "::warning::detect 가 fail-safe(full)로 내려앉아 state 를 신뢰할 수 없다 — "
            "갱신하지 않는다."
        )
        return 0

    missing = [k for k in _SAVE_STATE_REQUIRED_META_KEYS if k not in meta]
    if missing:
        print(f"::warning::meta.json 에 필요한 키가 없다({missing}) — 갱신하지 않는다.")
        return 0

    execution_file_content: str | None = None
    if args.execution_file:
        exec_path = Path(args.execution_file)
        if exec_path.exists():
            execution_file_content = exec_path.read_text(encoding="utf-8")

    ok, reason = is_review_successful(args.claude_outcome, execution_file_content)
    print(f"is_review_successful={ok} reason={reason}")
    if not ok:
        print("Claude 리뷰가 성공적으로 완료되지 않았다 — reviewed state 를 갱신하지 않는다.")
        return 0

    state = {
        "version": 1,
        "reviewed_head": meta["cur_head"],
        "reviewed_base": meta["cur_base"],
        "reviewed_base_tip": meta["cur_base_tip"],
        "patch_id": meta["patch_id"],
        "patch_sha256": meta["patch_sha256"],
        "file_fingerprints": meta["cur_fp"],
        "mode": mode,
        "run_id": str(meta["run_id"]),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    body = render_state_comment(state)

    if args.comment_out_file:
        Path(args.comment_out_file).write_text(body, encoding="utf-8")
        print(f"dry-run: 코멘트 본문을 {args.comment_out_file} 에 썼다.")
        return 0

    existing_comment_id = meta.get("existing_state_comment_id")
    try:
        if existing_comment_id:
            gh_patch_comment(args.repo, existing_comment_id, body)
        else:
            gh_create_comment(args.repo, meta["pr_number"], body)
    except subprocess.CalledProcessError as exc:
        print(
            f"::warning::reviewed state 코멘트 쓰기 실패 — 다음 실행은 state 없음(full)으로 "
            f"안전하게 fallback 된다. stderr={exc.stderr!r}"
        )
        return 0

    print("reviewed state 코멘트를 갱신했다.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="리뷰 모드 판별")
    detect.add_argument("--repo-dir", default=".")
    detect.add_argument("--workspace", required=True)
    detect.add_argument("--event-action", required=True)
    detect.add_argument("--base-ref", required=True)
    detect.add_argument("--head-sha", required=True)
    detect.add_argument("--pr-number", type=int, required=True)
    detect.add_argument("--repo", help="owner/name (gh api 용, --state-body-file 없을 때 필요)")
    detect.add_argument("--run-id", default="")
    detect.add_argument("--github-output", default="")
    detect.add_argument("--github-step-summary", default="")
    detect.add_argument(
        "--state-body-file",
        default="",
        help="주입/dry-run 용 — 있으면 gh api 호출 대신 이 파일을 state 코멘트 본문으로 쓴다"
        "(파일이 없으면 '코멘트 없음'으로 취급).",
    )
    detect.set_defaults(func=cmd_detect)

    save_state = sub.add_parser("save-state", help="Claude 리뷰 성공 시 reviewed state 갱신")
    save_state.add_argument("--meta-file", required=True)
    save_state.add_argument("--repo", default="")
    save_state.add_argument("--claude-outcome", required=True)
    save_state.add_argument("--execution-file", default="")
    save_state.add_argument(
        "--comment-out-file",
        default="",
        help="주입/dry-run 용 — 있으면 gh api 호출 대신 렌더된 코멘트 본문을 이 파일에 쓴다.",
    )
    save_state.set_defaults(func=cmd_save_state)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
