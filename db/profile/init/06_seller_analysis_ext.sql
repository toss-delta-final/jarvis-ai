-- 판매자 분석 저장 계층 확장 (이슈 #599 — R-1/R-2 보고서 조회 API)
-- 05_seller_analysis.sql 은 수정하지 않는다(파일 규약: 추가만) — 이 파일이 그 위에 얹힌다.
-- 전부 IF NOT EXISTS 라 두 번 적용해도 안전하다.
--
-- app/agents/seller/analysis_store.py 의 _run_ddl() 이 같은 문장을 복제한다 —
-- 이 파일이 DDL 정본이며, 변경 시 두 곳을 **같은 커밋**에서 갱신한다
-- (docs/specs/DESIGN-SELLER-ANALYSIS-STORE-585.md §9 "DDL이 두 곳에 존재" 위험 대응).
--
-- CHECK 제약은 두지 않는다 — 어휘는 애플리케이션 Literal 이 강제한다(05_*.sql 과 동일 원칙).

-- ① 보고서: findings 합성 결과 (부록 A)
--
-- FE AnalysisReport.tsx 가 `const structured = !!report.findings` 한 줄로 화면 모드를
-- 가른다 — 이 값이 비면 헤더(제목·생성시각·기간 배지·PDF 저장)가 통째로 렌더되지 않고
-- "구버전 서버 폴백" degrade 경로로 빠진다. 즉 장식이 아니라 정상 렌더의 최소 조건이다.
--
-- 조회 시점에 report_md 를 파싱해 만들지 않고 **생성 시점에 코드가 합성해 저장**한다:
-- 이슈 #599 완료 조건이 "페이지가 본문 파싱 없이" 그릴 수 있는 구조를 요구한다.
-- 모양은 schemas.AnalysisFinding 목록(snake_case) — S-4 report 이벤트와 같은 형식이라
-- 채팅과 페이지가 같은 조립기(_report_payload)를 공유할 수 있다.
ALTER TABLE seller_analysis_reports
  ADD COLUMN IF NOT EXISTS findings jsonb NOT NULL DEFAULT '[]';

-- ② 분석 대상: 배치 실행 기록 (R-1 noReportReason 판정 재료, 결정 113)
--
-- 보고서가 없을 때 이유가 여러 가지인데 와이어에서는 전부 items:[] 로 똑같이 보인다.
-- not_registered(대상 미등록 = 사고)와 no_trigger(이상 없음 = 정상)를 구분하지 못하면
-- 운영 사고가 몇 주간 드러나지 않는다.
--
-- last_run_at IS NULL = 등록은 됐으나 배치가 한 번도 안 돈 상태.
--   신규 판매자가 반드시 한 번 거치는 정상 상태이므로 not_registered 로 뭉뚱그리지
--   않는다 — R-1 은 이 경우 pending_first_run 을 돌려준다.
-- last_skip_reason 어휘: no_trigger | no_baseline (경량 스캔이 기록 — 이슈 16)
--   값이 없거나 알 수 없으면 R-1 은 **추정하지 않고 null 을 반환**한다.
--   "이상 없음"이라 단정하면 "판정 보류 != 이상 없음" 불변 규약을 와이어에서 깬다.
ALTER TABLE seller_analysis_targets
  ADD COLUMN IF NOT EXISTS last_run_at timestamptz;
ALTER TABLE seller_analysis_targets
  ADD COLUMN IF NOT EXISTS last_skip_reason text;
