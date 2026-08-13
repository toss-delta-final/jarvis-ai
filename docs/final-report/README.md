# Jarvis AI 최종 보고서 빌드 안내

## 파일

- `evidence.md` — 보고서 주장·수치와 저장소 근거의 대응표
- `report.html` — 편집 가능한 보고서 원본
- `report.css` — A4 인쇄 스타일과 한글 글꼴 설정
- `최종_보고서_완성본.pdf` — 제출용 PDF
- `../presentation/2026-08-13-evaluation-metrics-final.md` — 발표 평가·성능 파트 4페이지 구성,
  테스트셋 설계, #631 미병합 결과와 비용 단가 주의사항

## 요구 환경

- WeasyPrint 68.1
- Windows 한글 글꼴
  - `/mnt/c/Windows/Fonts/malgun.ttf`
  - `/mnt/c/Windows/Fonts/malgunbd.ttf`
- 검증용 Python 모듈: `pypdf`, `PyMuPDF`

글꼴 경로가 다른 환경에서는 `report.css`의 `@font-face` URL을 해당 시스템의 한글 글꼴로
바꾼다. 보고서에는 외부 네트워크 이미지가 없으므로 글꼴만 준비되면 오프라인에서 렌더링된다.

## PDF 생성

저장소 루트에서 실행한다.

```bash
weasyprint --pdf-identifier jarvis-final-report-20260813 \
  docs/final-report/report.html \
  docs/final-report/최종_보고서_완성본.pdf
```

## 구조·텍스트 검증

```bash
python3 - <<'PY'
from html.parser import HTMLParser
from pathlib import Path
from pypdf import PdfReader

class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chapters = 0
        self.figures = 0
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        self.chapters += tag == "section" and "chapter" in classes
        self.figures += tag == "figure"
        self.tables += tag == "table"

html = Path("docs/final-report/report.html").read_text(encoding="utf-8")
structure = Structure()
structure.feed(html)
assert (structure.chapters, structure.figures) == (8, 3)
assert structure.tables >= 5

pdf = Path("docs/final-report/최종_보고서_완성본.pdf")
reader = PdfReader(pdf)
text = "\n".join(page.extract_text() or "" for page in reader.pages)
assert 12 <= len(reader.pages) <= 16
for heading in ("1. 프로젝트 개요", "4. AI Agent 설계", "7. 테스트 및 결과", "8. 결론 및 향후 개선"):
    assert heading in text
assert pdf.stat().st_size > 100_000
print({"pages": len(reader.pages), "bytes": pdf.stat().st_size})
PY
```

## 페이지 렌더 검증

```bash
rm -rf /tmp/jarvis-final-report-render
mkdir -p /tmp/jarvis-final-report-render
python3 - <<'PY'
import fitz

doc = fitz.open("docs/final-report/최종_보고서_완성본.pdf")
for index, page in enumerate(doc):
    pix = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
    pix.save(f"/tmp/jarvis-final-report-render/page-{index + 1:02d}.png")
print(f"rendered {len(doc)} pages")
PY
```

모든 페이지 이미지를 확인해 글꼴 깨짐, 텍스트·표·도식 잘림, 겹침, 빈 spill 페이지가 없는지
검토한다. 최종 PDF를 원본 폴더로 복사한 뒤 저장소 PDF와 `cmp`로 동일성을 확인한다.
