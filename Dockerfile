# 멀티스테이지 uv 빌드 (결정 13). 임베딩(google-genai·pgvector)은 main deps라 uv sync 로 설치
# — 셀프호스트 torch·`--group embedding` 폐기(api-spec §4.8 v0.15.14).

# ── builder ──
FROM python:3.12-slim AS builder

# uv 바이너리를 공식 이미지에서 복사한다.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 잠금 파일 기준 재현 가능 설치 — dev 제외(임베딩 의존성은 main deps).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 프로젝트 소스 복사 후 프로젝트 자체 설치.
# README.md 는 pyproject `readme` 필드라 wheel 빌드(hatchling) 시 필요.
COPY README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── final ──
FROM python:3.12-slim AS final

# 비루트 사용자.
RUN groupadd --system jarvis && useradd --system --gid jarvis --create-home jarvis

WORKDIR /app

# 가상환경과 소스만 반입.
COPY --from=builder --chown=jarvis:jarvis /app/.venv /app/.venv
COPY --from=builder --chown=jarvis:jarvis /app/app /app/app
# db/ 는 런타임 의존이다 — session_context.initialize() 가 부팅 시
# db/profile/init/03_chat_session_contexts.sql 을 파일로 읽어 실행한다(이슈 #319).
# 빠뜨리면 컨테이너에서 FileNotFoundError 로 부팅이 실패한다.
COPY --chown=jarvis:jarvis db /app/db

ENV PATH="/app/.venv/bin:$PATH"

# [이슈 #583] 컨테이너 TZ 를 KST 로 고정한다 — 로그 타임스탬프(`%(asctime)s` 는 로컬 TZ 를
# 따른다)를 팀이 읽는 KST 로 맞추고, TZ 를 가정하는 코드가 새로 들어와도 안전하도록.
# 여기(이미지 ENV)에 두는 이유: EC2 배포는 `docker run --env-file` 이라 env 파일에 TZ 가
# 없으면 이 값을 그대로 물려받는다 — 배포·compose·로컬이 한 곳에서 덮인다.
# 계산의 기준 시각은 이 값에 기대지 않는다(app/core/clock.py 가 명시 오프셋으로 계산).
# python:3.12-slim 은 /usr/share/zoneinfo 를 포함해 tzdata 추가 설치가 필요 없다.
ENV TZ=Asia/Seoul

USER jarvis

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
