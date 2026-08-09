"""로깅 설정.

MVP 단계에서는 표준 logging 구성만 제공한다. 구조화 로깅(JSON)·request_id 상관관계
(api-spec §2.4 error.request_id)는 SPEC 단계에서 미들웨어로 보강한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from app.core.config import get_settings


def configure_logging(level: int = logging.INFO) -> None:
    """루트 로거 기본 구성. 앱 부팅 시 1회 호출한다."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    """모듈별 로거 획득 헬퍼."""
    return logging.getLogger(name)


def log_structured(logger: logging.Logger, event: str, /, **fields: object) -> None:
    """구조화 이벤트를 JSON message와 LogRecord extra에 함께 남긴다."""
    logger.info(
        json.dumps({"event": event, **fields}, ensure_ascii=False, default=str),
        extra={"event": event, **fields},
        stacklevel=2,
    )


def safe_fingerprint(value: str | None) -> str | None:
    """서버 pepper 기반 로그 상관관계 지문."""
    if value is None:
        return None
    pepper = get_settings().pii_hash_pepper.encode("utf-8")
    return hmac.new(pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
