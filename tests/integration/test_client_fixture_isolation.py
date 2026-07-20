"""client 픽스처 격리 회귀 가드 (PR #42 리뷰) — lifespan 이 진짜 스케줄러를 띄우면 안 된다.

app/main.py 가 lifespan 에서 start_scheduler()/stop_scheduler() 를 호출하게 된 뒤로,
tests/integration/conftest.py 의 client 픽스처(`with TestClient(app) as ...`)가 모든
통합 테스트마다 실제 BackgroundScheduler 를 기동·종료했다(간격 300s 라 실행 도중 실제
호출까지는 안 갔지만, 우연에 기대는 구조였다) — client 픽스처가 명시적으로 no-op
처리하도록 고쳐 이 위험을 구조적으로 없앤다. 스케줄러 자체의 동작 검증은
tests/unit/test_scheduler.py·test_main_lifespan.py 소관.
"""

from __future__ import annotations

from app.pipelines import scheduler as sched_mod


def test_client_fixture_does_not_start_real_scheduler(client) -> None:
    assert sched_mod._scheduler is None
