# 기준선 색인 (#462)

`evals/taste_probe/baselines/` 아래 실 LLM 기준선 판이 쌓이는 자리다(`evals/underspecified_probe/
baselines/README.md` 와 같은 색인 규약). **인용 대상은 하나여야 한다** — 이 문서가 그 하나를
가리킨다.

## 정본 선언

> ⚠️ **현재 커밋된 실 LLM 기준선이 없다.** 아래는 "기준선 부재" 상태의 색인이다 — 이 판에서
> 첫 기준선이 생기면 이 절을 실제 정본 표로 바꿔라.

| 항목 | 값 |
|---|---|
| 정본 디렉터리 | (없음) |
| 상태 | 실 LLM 경로 없음 — 이 환경에서 기준선을 아직 만들 수 없다 |

## 왜 없는가 — 실측한 오류 그대로

확인 시각: **2026-08-08**. 확인 방법: 단발 `llm.complete()` 호출을 provider 별로 직접 실행
(하네스 CLI 전체 런이 아니라 최소 재현 — 30~150콜을 헛되이 태우지 않기 위해).

**`LLM_PROVIDER=openai`(smart 모델 `gpt-5.6-luna`, 이 환경의 기본 설정):**

```
LLMError / cause RateLimitError
Error code: 429 - insufficient_quota
"You have no credits remaining. Add credits to continue using the API at
https://platform.openai.com/settings/organization/billing/."
```

**대안으로 `LLM_PROVIDER=anthropic`(smart 모델 `claude-sonnet-5`)도 확인했다 — 역시 안 된다:**

```
LLMError
Error code: 401 - authentication_error
"invalid x-api-key"
```

즉 이 환경에는 **쓰이는 실 LLM 경로가 없다.** 두 오류 모두 코드 결함이 아니라 계정/키 상태다
(크레딧 소진·키 무효) — 크레딧 충전이나 키 교체는 사람의 결정이고 워커 권한 밖이라 여기서
시도하지 않았다. `.env` 내용이나 키 값은 이 문서에도, 다른 어떤 산출물에도 적지 않는다 — 위
오류 메시지는 provider 가 돌려준 공개 오류 문면 그대로다.

## 이슈 완료 조건과의 관계

#462 완료 조건 4개는 **이 기준선 없이도 전부 충족된다**:

- [x] 미탐율·오탐율이 슬라이스별로 **기준선과 함께** 산출된다 — `trivial baseline`(§`baseline.py`,
      LLM 콜 0)이 그 몫이고, 이미 `results.json`/`report.md` 에 나란히 실린다.
- [x] 하네스가 프로덕션 함수를 직접 호출한다(판정 복제 0) — `runner.py`.
- [x] CI 에서 실 LLM 콜 0 으로 통과한다 — `tests/unit/test_taste_probe_*.py`(가짜만).
- [x] `uv run ruff check` · `uv run pytest` 통과.

작업 범위 항목 중 **"최초 기준선 산출물을 커밋한다"** 하나만 이 환경 제약으로 미충족이다 — 숨기지
않고 이 문서·`evals/taste_probe/README.md`·`CHANGELOG.md` 세 곳에 그 사실을 남긴다.

## 전제 — 라이브 pre-flight

첫 기준선을 만들기 전에 `loader.preflight_check_catalog` 가 라이브 pg-catalog 를 확인한다.
**이미 기동 중이고 시드도 채워져 있음을 확인했다**(`categories` 1,007행 + 임베딩 1,007행,
`docker exec jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog -tAc "select count(*) from
categories"` 로 실측). 이 부분은 준비돼 있다 — 막힌 것은 오직 LLM 호출 쪽이다.

## 기준선을 만들 때 — 그대로 복사해 쓸 명령

```bash
# 1) 배선 확인(30콜) — 실패하면 원인 없이 재시도하지 말고 멈춰서 원인을 잡는다
uv run python -m evals.taste_probe --out /tmp/tp-smoke-n1 --n 1

# 2) 기준선(150콜, 종료 코드 0 이어야 한다)
uv run python -m evals.taste_probe --out artifacts/tp-n5 --n 5
```

성공하면:

1. `artifacts/tp-n5/` 를 `evals/taste_probe/baselines/<provider>-<YYYYMMDD>-n5/` 로 옮긴다
   (예: `openai-20260815-n5`).
2. 이 문서의 「정본 선언」표를 실제 값으로 채운다 — **프롬프트 sha12**
   (`run_manifest.json.hashes.deltaSystemPrompt` 앞 12자, `report.md` 첫 줄에도 실린다) ·
   `datasetVersion` · `datasetHash`(`fixture.sha256` 앞 12자) · provider · 모델 · N.
3. 「왜 없는가」절을 지우거나 "과거 상태(해소됨)"로 표시만 남긴다 — 오류 재현 기록 자체는
   나중에 같은 문제가 재발했을 때 참고가 되므로 완전히 지우지 않는 것을 권한다.

## ⚠ 다른 해시끼리 비교 금지 (#328 규약 8)

`hashes.deltaSystemPrompt`·`hashes.datasetFixture` 가 다른 두 판을 나란히 놓고 "개선/퇴행"을
말하지 마라 — 프롬프트나 데이터셋이 바뀌면 그 자체로 다른 측정이다. 새 판을 추가할 때마다 이
표에 해시를 함께 적어, 다음 사람이 실수로 세대가 다른 판을 비교하지 않게 한다.

## 단일 실행은 판정이 아니다

`evals/taste_probe/README.md` 「표본 사전 산정」절의 군집 표본 경고가 그대로 적용된다 — 첫
기준선이 생겨도 그 한 판만으로 "추출이 좋다/나쁘다"를 판정하지 않는다. 독립 2~3판이 쌓이기
전까지는 방향 판정용으로만 쓴다.
