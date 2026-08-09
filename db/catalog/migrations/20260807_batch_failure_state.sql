-- pg-catalog I-17 배치 2선·3선 연속 실패 스트릭 영속화(이슈 #416) — batch_failure_state 신설.
--
-- 스케줄러 프로세스가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작되면(연속 배포·크래시 루프)
-- 프로세스 메모리 스트릭이 매번 0으로 리셋돼 poison 상품이 dead-letter 상한에 영영 도달하지
-- 못했다. app/pipelines/pg_artifact_store.py 가 이 테이블을 원자 UPSERT 로 읽고 쓴다.
--
-- 기존 볼륨에 반복 적용해도 안전한 수동 migration. 배포 인스턴스는 이 파일로 in-place 적용한다.
-- (앱도 첫 사용 시 같은 DDL 을 idempotent 하게 자가 적용하므로, 이 파일은 배포 시점에 미리
-- 반영해 두기 위한 것일 뿐 필수 선행 조건은 아니다.)

BEGIN;

CREATE TABLE IF NOT EXISTS batch_failure_state (
    kind       text        NOT NULL,  -- "item"(key=product_id) | "page"(key=cursor)
    state_key  text        NOT NULL,
    streak     int         NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, state_key)
);

COMMIT;
