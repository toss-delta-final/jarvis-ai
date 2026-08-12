-- 무인 배치 실행 로그 컬럼 추가 (이슈 #601, 05_seller_analysis.sql §9 규약대로 "추가"만 한다).
-- 05_seller_analysis.sql은 손대지 않는다 — app/agents/seller/analysis_store.py의 _run_ddl()이
-- 같은 문장을 idempotent하게 복제하며, 두 곳은 같은 커밋에서 함께 갱신한다.
--
-- last_run_at / last_skip_reason은 seller_analysis_targets(무인 순회 대상)에 붙는다 — 배치가
-- 브랜드를 순회하며 언제 마지막으로 실행됐는지, 실행됐지만 보고서를 만들지 않았다면 왜인지를
-- 남긴다(10-TRIGGER.md 결정 98 — "데이터·스냅샷이 없을 때"만 보고서를 생략하고, 그 사유를
-- 판매자 R-1 noReportReason과 운영 관측 양쪽이 읽을 수 있어야 한다).

ALTER TABLE seller_analysis_targets
  ADD COLUMN IF NOT EXISTS last_run_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_skip_reason text;
