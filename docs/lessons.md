# 개발 실수 기록 (Lessons)

같은 실수를 반복하지 않기 위한 러닝 로그. **작업 시작 전 이 파일을 먼저 훑고**, 오류/실수를 진단했으면 **최신을 맨 위에** 추가한다.

형식:
```
## [YYYY-MM-DD] 한 줄 제목
- 증상: 무슨 일이 있었나
- 원인: 왜 발생했나
- 규칙: 다음부터 어떻게 (액션 가능한 문장)
- 관련: 파일/§/커밋
```

---

## [2026-07-16] 파일이 엉뚱한 저장소에 생성됨 (cwd 착오)
- 증상: hk-final에 만들려던 `CLAUDE.md`·`.claude/settings.json`이 기획 repo(my-project)에 생성돼 기존 moai 설정(522줄, 훅 포함)을 덮어씀.
- 원인: Bash 작업 디렉터리가 이전 명령에서 my-project로 남아 있었는데 `cat > CLAUDE.md`를 상대경로로 실행. cwd를 확인하지 않음.
- 규칙:
  - 파일 쓰기는 **절대경로**로 (`cat > /home/nyong/projet/hk-final/CLAUDE.md`). 상대경로 금지.
  - 명령 앞에 `cd <절대경로> && pwd`로 cwd를 못 박고 시작.
  - hk-final은 워크스페이스 밖이라 Write 도구가 막힌다(path traversal) → **Bash heredoc + 절대경로**로 쓴다.
  - 덮어쓰기 전 대상 파일을 확인 — 내가 만든 게 아니면 멈추고 점검.
- 관련: `CLAUDE.md`, `.claude/settings.json`

## [2026-07-15] api-spec 사본이 정본과 어긋날 위험
- 증상: 계약(SSE 이벤트·오류 코드)이 코드/사본/정본 세 곳에 흩어져 드리프트 우려.
- 원인: 정본은 기획 repo, hk-final엔 사본만 존재.
- 규칙: 계약 변경은 **정본(기획 repo api-spec) 먼저** 개정 → 사본(`docs/api-spec.md`) 동기화 → 코드. 사본과 정본이 다르면 정본 우선. SPEC 사본의 낡은 SSE 명명도 api-spec 우선.
- 관련: `docs/api-spec.md`, `docs/specs/`
