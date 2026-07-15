# 멀티스테이지 uv 빌드 (결정 13). 임베딩 그룹 포함 (파이프라인/런타임 필요).

# ── builder ──
FROM python:3.12-slim AS builder

# uv 바이너리를 공식 이미지에서 복사한다.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 잠금 파일 기준 재현 가능 설치 — dev 제외, embedding 그룹 포함.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group embedding --no-install-project

# 프로젝트 소스 복사 후 프로젝트 자체 설치.
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group embedding

# ── final ──
FROM python:3.12-slim AS final

# 비루트 사용자.
RUN groupadd --system jarvis && useradd --system --gid jarvis --create-home jarvis

WORKDIR /app

# 가상환경과 소스만 반입.
COPY --from=builder --chown=jarvis:jarvis /app/.venv /app/.venv
COPY --from=builder --chown=jarvis:jarvis /app/app /app/app

ENV PATH="/app/.venv/bin:$PATH"

USER jarvis

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
