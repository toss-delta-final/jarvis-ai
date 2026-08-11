"""서비스 기준 시각(KST) 단일 출처 — 컨테이너 TZ 와 무관하게 동작한다.

[이슈 #583] 배경: `report.generatedAt` 은 KST 로 내보내면서(api-spec §3.2 v0.24.0,
이슈 #296) 기간 해석의 "오늘" 은 `date.today()` = 컨테이너 로컬 TZ 였다. 운영
컨테이너가 UTC 라 00~09 KST 사이에 판매자가 "어제 매출" 을 물으면 이틀 전 데이터가
나갔다. 기준 시각을 이 모듈 하나로 모아 그 어긋남을 없앤다.

**TZ 환경변수에 기대지 않는다.** 배포에 `TZ=Asia/Seoul` 을 고정하더라도(Dockerfile·
docker-compose) 그건 로그 가독성·이중 안전장치일 뿐이고, 코드는 TZ 가 무엇이든 같은
날짜를 계산해야 한다 — 그래야 로컬(UTC WSL)·CI·운영이 동일하게 동작한다.

`ZoneInfo("Asia/Seoul")` 대신 고정 오프셋을 쓰는 이유:
  · 한국은 1988년 이후 DST 가 없어 상시 +09:00 — 두 방식의 결과가 같다.
  · OS tzdata 에 의존하지 않아 slim 이미지에서 실패할 여지가 없다.
  · 기존 `_KST = timezone(timedelta(hours=9))` 의 직렬화 결과(`+09:00`)가 바이트
    단위로 보존된다. FE(AnalysisReport.tsx `formatGeneratedAt`)가 `generatedAt` 을
    정규식으로 앞부분만 잘라 쓰므로 오프셋 표기가 바뀌면 화면 시각이 틀어진다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# 서비스 기준 타임존 — 판매자 기간 해석·report.generatedAt 의 단일 출처.
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    """KST aware 현재 시각. 프로세스 TZ 와 무관하다."""
    return datetime.now(KST)


def today_kst() -> date:
    """KST 기준 오늘 날짜.

    `date.today()` 를 대체한다 — 후자는 컨테이너 로컬 TZ 를 따르므로 UTC 컨테이너에서
    09시 이전에는 KST 기준 하루 전을 돌려준다(#583).
    """
    return now_kst().date()
