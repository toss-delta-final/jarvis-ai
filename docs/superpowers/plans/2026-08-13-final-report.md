# Jarvis AI 최종 보고서 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jarvis AI 저장소의 실제 구현과 평가 근거를 기반으로 편집 가능한 원본과 제출용 한국어 PDF 최종 보고서를 만든다.

**Architecture:** 정량 주장과 출처를 먼저 evidence ledger에 고정하고, A4 인쇄 전용 HTML/CSS에 8개 장과 벡터 도식을 작성한다. 로컬에 설치된 WeasyPrint로 PDF를 생성한 뒤 텍스트·페이지 수·렌더 이미지·원본 보존을 검증하고 동일 PDF를 원본 폴더와 저장소에 배치한다.

**Tech Stack:** HTML5, CSS Paged Media, inline SVG, WeasyPrint 68.1, Python 3.12, pypdf, PyMuPDF, Malgun Gothic

## Global Constraints

- 원본 `/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/90e4841b-671d-4914-8c87-c2ebba441739_최종_보고서.pdf`는 수정하지 않는다.
- 최종 PDF는 약 12~15페이지의 A4 세로형 한국어 정식 보고서로 만든다.
- 애플리케이션 코드와 프로젝트 의존성은 변경하지 않는다.
- 저장소 코드·문서·테스트·평가 artifact에서 확인되지 않은 팀 정보나 성과는 만들지 않는다.
- 측정값은 평가 범위와 한계를 함께 표시한다.
- 기존 작업 트리의 사용자 변경은 스테이징하거나 수정하지 않는다.

---

### Task 1: 보고서 근거 원장 고정

**Files:**
- Create: `docs/final-report/evidence.md`

**Interfaces:**
- Consumes: `README.md`, `docs/api-spec.md`, `data-analysis/REPORT.md`, `evals/README.md`, `evals/adversarial_recommendation/README.md`, `evals/rerank_grounding/README.md`, `evals/benchmark/README.md`, 관련 JSON/Markdown baseline artifact
- Produces: 보고서의 각 장과 정량 주장에 대응하는 출처 경로, 측정 조건, 사용 가능한 문구, 금지된 과장 범위

- [ ] **Step 1: 아키텍처와 기능 근거를 수집한다**

Run:

```bash
rg -n "에이전틱 커머스|3-tier|경로 B|핵심 기능|기술 스택" README.md
rg -n "POST /chat|POST /seller/chat|products.ready|session-end" docs/api-spec.md README.md
find app/agents/{buyer,seller,profile} -type f -name '*.py' | sort
```

Expected: Buyer/Seller/Profile 경계와 React–AI–Spring 책임 분리가 확인된다.

- [ ] **Step 2: 테스트와 평가 수치를 원 artifact에서 확인한다**

Run:

```bash
rg -n "210 family|450 case|20%|42 family" evals/adversarial_recommendation/README.md evals/README.md
rg -n "0/80|0/212|0/208|6.81%|571 attempts|511,192" evals/rerank_grounding/README.md
rg -n "총 이벤트|사용자|세션|상품|전환|재구매" data-analysis/REPORT.md
```

Expected: 보고서에 사용할 수치와 각 수치의 제한 조건이 원문에서 확인된다.

- [ ] **Step 3: 근거 원장을 작성한다**

Write `docs/final-report/evidence.md` with these columns:

```markdown
| 보고서 위치 | 주장 또는 수치 | 근거 파일 | 사용 조건·한계 |
|---|---|---|---|
| 3장 | AI 서버는 판단과 조율, Spring은 원본과 트랜잭션 권위를 담당 | README.md | 이 저장소가 구현한 범위만 설명 |
| 7장 | adversarial dataset은 210 family, 450 case | evals/adversarial_recommendation/README.md | 자동 규칙과 사람 검토 범위 구분 |
| 7장 | validated의 등록된 unsupported evidence는 세 run에서 0건 | evals/rerank_grounding/README.md | 평가한 근거군에만 한정 |
```

Every quantitative statement planned for the report must have one row.

- [ ] **Step 4: 근거 원장의 금지 문구와 누락을 검사한다**

Run:

```bash
rg -n 'TBD|TODO|FIXME|\[[^]]*(이름|수치|링크)[^]]*\]' docs/final-report/evidence.md && exit 1 || true
rg -n '^\| [1-8]장 ' docs/final-report/evidence.md
```

Expected: 임시 문구가 없고 1~8장 근거가 모두 존재한다.

- [ ] **Step 5: 근거 원장을 커밋한다**

```bash
git add docs/final-report/evidence.md
git commit -m "docs(report): 검증 가능한 주장만 최종 보고서에 허용한다" \
  -m "Constraint: 저장소와 등록된 평가 artifact만 근거로 사용" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Directive: 수치에는 평가 범위와 한계를 함께 표시할 것" \
  -m "Tested: evidence source and placeholder scans" \
  -m "Not-tested: PDF layout"
```

### Task 2: A4 보고서 원본과 스타일 작성

**Files:**
- Create: `docs/final-report/report.html`
- Create: `docs/final-report/report.css`
- Create: `docs/final-report/README.md`

**Interfaces:**
- Consumes: `docs/final-report/evidence.md`, `docs/superpowers/specs/2026-08-13-final-report-design.md`
- Produces: WeasyPrint가 직접 렌더링할 수 있는 8개 장의 HTML 원본, 인쇄 스타일, 재생성 명령

- [ ] **Step 1: 인쇄 스타일의 검증 기준을 먼저 작성한다**

Create `docs/final-report/report.css` with:

```css
@page { size: A4; margin: 17mm 16mm 18mm; }
@page { @bottom-left { content: "Jarvis AI 최종 보고서"; } }
@page { @bottom-right { content: counter(page); } }
html { font-family: "Malgun Gothic", "D2Coding", sans-serif; color: #172033; }
.page-break { break-before: page; }
.avoid-break, table, figure { break-inside: avoid; }
```

Add explicit styles for cover, chapter header, two-column cards, tables, callouts, diagrams, and references. Use `@font-face` with the existing `/mnt/c/Windows/Fonts/malgun.ttf` and `malgunbd.ttf` files so Korean glyphs are embedded.

- [ ] **Step 2: 보고서의 전체 구조를 작성한다**

Create `docs/final-report/report.html` with this exact order:

```text
표지 → 초록/핵심 성과 → 목차 → 1. 프로젝트 개요 → 2. 문제 정의 및 요구사항
→ 3. 서비스/시스템 설계 → 4. AI Agent 설계 → 5. 핵심 기능 구현
→ 6. 데이터 및 검색 시스템 → 7. 테스트 및 결과 → 8. 결론 및 향후 개선 → 참고 근거
```

Use semantic `section`, `h1`–`h3`, `table`, `figure`, and inline SVG elements. Include file paths in compact source notes rather than raw code screenshots.

- [ ] **Step 3: 세 가지 핵심 도식을 inline SVG로 작성한다**

Add:

```text
도식 1: 사용자 → React FE → Jarvis AI → Spring BE → 저장소/LLM 전체 아키텍처
도식 2: intent routing → recommendation/cart/order/fallback Buyer graph
도식 3: 구조화 필터 → 후보 검색 → semantic/attribute 압축 → rerank → validator → push
```

All SVGs must have a `viewBox`, text labels, arrows, and a caption. No external network asset is allowed.

- [ ] **Step 4: 8개 장의 본문과 평가 표를 근거 원장에서 채운다**

6장에서는 구조화 필터, 임베딩, PostgreSQL·pgvector, rerank의 역할과 데이터 소유 경계를
구분하고, REES46 통계는 운영 성과가 아니라 데이터셋 구성 근거로만 설명한다.

Required measured-result rows:

```text
Adversarial recommendation: 210 family / 450 case / 42 family direct review
Rerank grounding C: unsupported evidence 0/80, 0/212, 0/208
Confirmation comparison: A 28/411 (6.81%), B 0/418, C 0/420
Guardrails: out-of-candidate ID 0, duplicate 0, post-validation invalid 0
Dataset basis: 42,448,764 events / 3,022,290 users / 9,244,422 sessions
```

Every table includes a nearby limitation note.

- [ ] **Step 5: 재생성 안내를 작성한다**

Create `docs/final-report/README.md` containing:

```bash
weasyprint docs/final-report/report.html docs/final-report/최종_보고서_완성본.pdf
```

Document the expected WeasyPrint version, Windows Malgun Gothic paths, output paths, and validation commands.

- [ ] **Step 6: HTML 구조와 임시 문구를 검사한다**

Run:

```bash
python3 - <<'PY'
from html.parser import HTMLParser
from pathlib import Path

class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chapter_count = 0
        self.figure_count = 0
        self.table_count = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'section' and 'chapter' in classes:
            self.chapter_count += 1
        self.figure_count += tag == 'figure'
        self.table_count += tag == 'table'

    def handle_data(self, data):
        self.text.append(data)

p = ReportParser()
p.feed(Path('docs/final-report/report.html').read_text(encoding='utf-8'))
assert p.chapter_count == 8, p.chapter_count
assert p.figure_count >= 3, p.figure_count
assert p.table_count >= 5, p.table_count
joined = ' '.join(p.text)
assert not any(token in joined for token in ('T' + 'BD', 'TO' + 'DO', '[이름]', '[최종 수치]'))
print('html structure OK')
PY
```

- [ ] **Step 7: 원본과 스타일을 커밋한다**

```bash
git add docs/final-report/report.html docs/final-report/report.css docs/final-report/README.md
git commit -m "docs(report): 구현과 평가 근거를 제출용 서사로 연결한다" \
  -m "Constraint: A4 한국어 보고서와 오프라인 렌더링" \
  -m "Rejected: 외부 이미지 자산 | 재현성과 오프라인 렌더링을 해침" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Directive: 정량 표의 한계 문구를 제거하지 말 것" \
  -m "Tested: HTML structure, chapter, figure, table, placeholder scans" \
  -m "Not-tested: final PDF pagination"
```

### Task 3: PDF 생성과 시각 검증

**Files:**
- Create: `docs/final-report/최종_보고서_완성본.pdf`
- Create: `/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/최종_보고서_완성본.pdf`

**Interfaces:**
- Consumes: `docs/final-report/report.html`, `docs/final-report/report.css`
- Produces: 저장소와 원본 폴더의 바이트 동일한 최종 PDF, 페이지별 렌더 검증 결과

- [ ] **Step 1: WeasyPrint로 PDF를 생성한다**

Run:

```bash
weasyprint --pdf-identifier jarvis-final-report-20260813 \
  docs/final-report/report.html docs/final-report/최종_보고서_완성본.pdf
```

Expected: command exits 0 and creates a non-empty PDF.

- [ ] **Step 2: 텍스트·페이지 수·메타데이터를 검사한다**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
from pypdf import PdfReader
p = Path('docs/final-report/최종_보고서_완성본.pdf')
r = PdfReader(p)
text = '\n'.join(page.extract_text() or '' for page in r.pages)
assert 12 <= len(r.pages) <= 16, len(r.pages)
for heading in ('1. 프로젝트 개요', '4. AI Agent 설계', '7. 테스트 및 결과', '8. 결론 및 향후 개선'):
    assert heading in text, heading
assert all(token not in text for token in ('TBD', 'TODO', '[이름]', '[최종 수치]'))
assert p.stat().st_size > 100_000
print({'pages': len(r.pages), 'bytes': p.stat().st_size})
PY
```

Expected: 12~16 pages, all required headings, no placeholders, file size over 100 KB, and
한국어 텍스트가 페이지별로 추출된다.

- [ ] **Step 3: 모든 페이지를 PNG로 렌더한다**

Run:

```bash
rm -rf /tmp/jarvis-final-report-render && mkdir -p /tmp/jarvis-final-report-render
python3 - <<'PY'
import fitz
doc = fitz.open('docs/final-report/최종_보고서_완성본.pdf')
for index, page in enumerate(doc):
    pix = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
    pix.save(f'/tmp/jarvis-final-report-render/page-{index + 1:02d}.png')
print(len(doc))
PY
```

Expected: one non-empty PNG for every PDF page.

- [ ] **Step 4: 연락처 시트와 대표 페이지를 육안 검사한다**

Create a contact sheet from all PNGs and inspect the cover, every chapter opening, dense tables, and final page. Reject and fix any text clipping, overlapping elements, orphan headings, nearly empty spill pages, or broken Korean glyphs.

- [ ] **Step 5: 원본 보존과 최종 사본의 동일성을 검증한다**

Run:

```bash
original='/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/90e4841b-671d-4914-8c87-c2ebba441739_최종_보고서.pdf'
sha256sum "$original" > /tmp/jarvis-original-before.sha256
cp docs/final-report/최종_보고서_완성본.pdf '/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/최종_보고서_완성본.pdf'
sha256sum -c /tmp/jarvis-original-before.sha256
cmp docs/final-report/최종_보고서_완성본.pdf '/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/최종_보고서_완성본.pdf'
```

Expected: original checksum remains valid and the two completed PDFs are byte-identical.

- [ ] **Step 6: PDF를 커밋한다**

```bash
git add docs/final-report/최종_보고서_완성본.pdf
git commit -m "docs(report): 검증된 최종 보고서를 재현 가능한 산출물로 남긴다" \
  -m "Constraint: 원본 PDF 보존과 저장소·제출 폴더의 바이트 동일성" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Directive: HTML 원본을 변경하면 PDF 렌더 검증도 다시 수행할 것" \
  -m "Tested: PDF text, pagination, page renders, checksum, byte comparison" \
  -m "Not-tested: physical printer output"
```

### Task 4: 최종 회귀와 전달 검증

**Files:**
- Verify: `docs/final-report/evidence.md`
- Verify: `docs/final-report/report.html`
- Verify: `docs/final-report/report.css`
- Verify: `docs/final-report/최종_보고서_완성본.pdf`

**Interfaces:**
- Consumes: 모든 보고서 산출물과 현재 저장소 테스트 체계
- Produces: 보고서 주장과 산출물 완성도를 뒷받침하는 최종 검증 로그

- [ ] **Step 1: 보고서 전용 정적 검사를 다시 실행한다**

```bash
rg -n 'TBD|TODO|FIXME|\[[^]]*(이름|수치|링크)[^]]*\]' docs/final-report && exit 1 || true
weasyprint --pdf-identifier jarvis-final-report-20260813 \
  docs/final-report/report.html /tmp/jarvis-final-report-rebuilt.pdf
python3 - <<'PY'
from pypdf import PdfReader

def content(path):
    reader = PdfReader(path)
    return len(reader.pages), [page.extract_text() or '' for page in reader.pages]

assert content('docs/final-report/최종_보고서_완성본.pdf') == content('/tmp/jarvis-final-report-rebuilt.pdf')
print('semantic rebuild OK')
PY
```

Expected: no placeholders and identical page count and extracted page text after rebuilding.

- [ ] **Step 2: 애플리케이션 정적 검사와 테스트를 실행한다**

```bash
uv run ruff check
uv run pytest -q
```

Expected: Ruff and the default non-smoke/non-integration/non-slow test suite pass. If unrelated pre-existing dirty changes cause failures, record exact failing tests without modifying those files.

- [ ] **Step 3: 최종 상태와 산출물 해시를 기록한다**

```bash
sha256sum docs/final-report/최종_보고서_완성본.pdf '/mnt/d/Shortcuts/Documents/카카오톡 받은 파일/최종_보고서_완성본.pdf'
git status --short --branch
git log -4 --oneline
```

Expected: hashes match, only pre-existing unrelated worktree changes remain, and all report commits are visible.
