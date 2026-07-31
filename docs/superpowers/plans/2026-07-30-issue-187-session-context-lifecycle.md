# Issue #187 Session Context Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make buyer context ownership, activity, expiry and cleanup session-scoped
across all threads, including an atomic guest-to-member claim.

**Architecture:** Bind buyer tickets to a signed `sessionId`, resolve a globally unique
stable `context_id`, and key versioned buyer state by context/thread. A PostgreSQL
lifecycle repository owns current generation and finalization phases; a coordinator
serializes claim, idle/I-20 cleanup and profile processing while preserving seller
conversation behavior.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, psycopg/psycopg-pool, LangGraph
BaseStore, APScheduler, pytest/pytest-asyncio, Ruff, PostgreSQL.

## Global Constraints

- Do not deploy the AI enforcement change until BE #63 emits signed `sessionId` in
  every buyer ticket and all pre-contract tickets have expired.
- Keep `threadId` out of the ticket so one ticket supports multiple tabs.
- Current production invariant is one application process and one Uvicorn worker.
- Do not add a dependency.
- Do not feed transcript text into the buyer prompt or promote guest turns into
  member profile facts.
- Transient idle TTL is 600 seconds; metadata/transcript purge remains deferred to
  I-21.
- New commits follow the repository Lore commit protocol.

---

## File structure

### New files

- `app/core/session_context.py` — lifecycle types, PostgreSQL/fallback repository,
  session advisory locking, schema initialization and migration state.
- `app/core/session_lifecycle.py` — owner-claim, idle/terminal coordinator,
  active-profile task registry and phase transitions.
- `app/agents/buyer/session_state.py` — aggregate v2 thread-state clear/adoption and
  legacy GC.
- `db/profile/init/03_chat_session_contexts.sql` — context, thread, finalization,
  claim-history, migration and quarantine tables.
- `tests/unit/test_session_context.py`
- `tests/unit/test_session_context_cleanup.py`
- `tests/unit/test_session_context_adoption.py`
- `tests/unit/test_session_claim_api.py`
- `tests/integration/test_pg_session_context.py`
- `docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md`

### Modified files

- `app/core/auth.py`, `app/api/deps.py`, `app/api/chat.py` — signed session binding.
- `app/core/conversation.py`, `app/core/observability.py`, `app/core/stream.py` —
  optional buyer lifecycle commit result.
- `app/agents/buyer/graph.py`, `cart/state.py`, `recommendation/state.py` — v2 keys,
  clear/adoption operations.
- `app/agents/profile/finalizer.py`, `idle_timeout.py`, `session_activity.py` —
  profile phase extraction and legacy backfill-only status.
- `app/api/events.py`, `app/schemas/events.py`, `app/pipelines/scheduler.py`,
  `app/main.py` — claim/I-20 endpoints, unified sweep and ordered initialization.
- `app/core/config.py`, `app/core/pg_resilience.py` — rollout bounds and one-key LRU
  removal.
- `db/profile/init/01_conversation_turns.sql`, `docs/api-spec.md` — linkage and wire
  contract.
- Existing auth, seller, observability, conversation, profile, scheduler, cart and
  buyer integration tests.

---

### Task 0: Start from a fresh latest-`dev` worktree

**Files:**
- Read: `/home/nyong/inte-final/jarvis-ai`
- Create worktree: `/home/nyong/inte-final/.worktrees/jarvis-ai-187`

**Interfaces:**
- Produces an isolated `feat/187-session-context` branch at current `origin/dev`.
- The approved plan remains readable at
  `/home/nyong/inte-final/jarvis-ai/docs/superpowers/plans/2026-07-30-issue-187-session-context-lifecycle.md`.

- [ ] **Step 1: Fetch and prove the current checkout is stale**

```bash
cd /home/nyong/inte-final/jarvis-ai
git fetch origin --prune
git rev-list --left-right --count dev...origin/dev
```

Remote `dev` moved repeatedly during planning; treat the numbers as freshness
evidence, not a fixed expected value. Do not implement unless the worktree created in
Step 2 is `0 0`.

- [ ] **Step 2: Create the worktree from the remote tip**

Use the `superpowers:using-git-worktrees` skill, then run:

```bash
git worktree add /home/nyong/inte-final/.worktrees/jarvis-ai-187 \
  -b feat/187-session-context origin/dev
cd /home/nyong/inte-final/.worktrees/jarvis-ai-187
git status --short --branch
git rev-list --left-right --count HEAD...origin/dev
```

Expected: clean branch and `0 0`.

- [ ] **Step 3: Revalidate upstream overlap**

```bash
git diff --name-only 4ec3d33..origin/dev -- \
  app/core/config.py app/core/auth.py app/core/conversation.py \
  app/agents/profile app/api/events.py app/pipelines/scheduler.py \
  db/profile docs/api-spec.md tests
```

Read every listed changed file before Task 1 and preserve newer behavior/tests.

---

### Task 1: Bind buyer requests to the signed session claim

**Files:**
- Modify: `app/core/auth.py`
- Modify: `app/api/deps.py`
- Modify: `app/api/chat.py`
- Modify: `docs/api-spec.md`
- Test: `tests/unit/test_auth_e2e.py`
- Test: `tests/integration/test_auth_e2e_flow.py`
- Test: `tests/unit/test_seller_api.py`

**Interfaces:**
- Produces: `Identity.session_id: str | None`
- Produces: `require_buyer_session(identity: Identity, session_id: str, settings:
  Settings) -> None`
- Produces: `buyer_owner_id(identity: Identity, settings: Settings) -> str`
- Consumes later: Task 3 builds `BuyerSessionInput` only after this guard succeeds.

- [ ] **Step 1: Add failing claim parsing and route-guard tests**

```python
def test_buyer_session_claim_must_match_body(client, member_token_for_session):
    response = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {member_token_for_session('S1')}"},
        json={"sessionId": "S2", "threadId": "T1", "message": "hello"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SESSION_FORBIDDEN"
```

Also assert missing claim is 403 in `jwks` mode, a matching claim reaches the stream
path, and `/seller/chat` still accepts seller tickets without the claim.

- [ ] **Step 2: Run the focused tests and confirm the new cases fail**

```bash
uv run pytest -q tests/unit/test_auth_e2e.py \
  tests/integration/test_auth_e2e_flow.py \
  tests/unit/test_seller_api.py
```

Expected: new tests fail because `Identity` does not preserve or validate
`sessionId`; existing cases stay green.

- [ ] **Step 3: Add the claim and buyer-only guard**

```python
CLAIM_SESSION_ID = "sessionId"

@dataclass(frozen=True)
class Identity:
    user_id: str | None
    is_guest: bool
    seller_id: str | None
    brand_id: str | int | None = None
    subject: str | None = None
    session_id: str | None = None

def require_buyer_session(
    identity: Identity,
    session_id: str,
    settings: Settings,
) -> None:
    if settings.auth_mode == "dev" and identity.session_id is None:
        return
    if not identity.session_id or identity.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_FORBIDDEN", "message": "session access denied"},
        )

def buyer_owner_id(identity: Identity, settings: Settings) -> str:
    if identity.subject:
        return identity.subject
    if settings.auth_mode == "dev":
        return "dev-anon"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "SESSION_FORBIDDEN", "message": "session access denied"},
    )
```

Populate `session_id` in every `_claims_to_identity()` return. Call the guard in
`app/api/chat.py` before `get_conversation_store()` and before `open_stream()`.
Do not call it from seller routes. Add a dev no-token test for `dev-anon` and a dev
token mismatch test.

- [ ] **Step 4: Document the ticket and error contract**

In `docs/api-spec.md`, add signed `sessionId` to buyer stream tickets, explicitly keep
`threadId` body-only, and add 403 `SESSION_FORBIDDEN`.

- [ ] **Step 5: Re-run focused tests**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/auth.py app/api/deps.py app/api/chat.py docs/api-spec.md \
  tests/unit/test_auth_e2e.py tests/integration/test_auth_e2e_flow.py \
  tests/unit/test_seller_api.py
git commit -m "$(cat <<'EOF'
Bind buyer access to the session proven by its stream ticket

Constraint: threadId remains body-only so one ticket serves multiple tabs
Confidence: high
Scope-risk: moderate
Directive: deploy only after BE ticket rollout and old-ticket expiry
Tested: focused buyer auth, integration auth, and seller regression tests
Not-tested: cross-repository ticket issuance
EOF
)"
```

---

### Task 2: Add the lifecycle schema and repository

**Files:**
- Create: `db/profile/init/03_chat_session_contexts.sql`
- Create: `app/core/session_context.py`
- Modify: `db/profile/init/01_conversation_turns.sql`
- Modify: `app/core/config.py`
- Modify: `app/core/errors.py`
- Test: `tests/unit/test_session_context.py`
- Test: `tests/integration/test_pg_session_context.py`
- Test: `tests/unit/test_infra.py`

**Interfaces:**
- Produces:

```python
OwnerType = Literal["guest", "member"]
ContextState = Literal["active", "idle_finalizing", "idle_expired", "terminal"]

@dataclass(frozen=True)
class BuyerSessionInput:
    session_id: str
    thread_id: str
    owner_type: OwnerType
    owner_id: str

@dataclass(frozen=True)
class SessionContext:
    context_id: str
    session_id: str
    owner_type: OwnerType
    owner_id: str
    generation: int
    state: ContextState
```

- Produces repository operations:
  `resolve_touch_register_on_connection()`, `claim_owner()`,
  `claim_expired_contexts()`, `begin_terminal()`, `get_threads()`,
  `complete_transient_phase()`, `record_profile_phase()`, `initialize()`.
- Produces domain errors: `SessionForbidden`, `SessionFinalizing`,
  `SessionActive`, `SessionClaimConflict`, `SessionStateUnavailable`.
- Produces wire mapping in `app/core/errors.py`: 403 `SESSION_FORBIDDEN`; 409
  `SESSION_FINALIZING`, `SESSION_ACTIVE`, `SESSION_CLAIM_CONFLICT`; 503
  `STATE_UNAVAILABLE`.

- [ ] **Step 1: Write failing repository tests**

Cover global session uniqueness, guest/member creation, owner mismatch, idle
reactivation, terminal denial, generation increments, thread upsert, claim leases,
exact duplicate/conflict owner claims and fallback parity.

```python
async def test_touch_rejects_other_owner(repo):
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    with pytest.raises(SessionForbidden):
        await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
```

- [ ] **Step 2: Run unit tests and confirm failure**

```bash
uv run pytest -q tests/unit/test_session_context.py
```

Expected: import failure for the new module.

- [ ] **Step 3: Create exact tables and constraints**

The SQL must create:

```sql
CREATE TABLE IF NOT EXISTS chat_session_contexts (
    context_id uuid PRIMARY KEY,
    session_id text NOT NULL UNIQUE,
    owner_type text NOT NULL CHECK (owner_type IN ('guest', 'member')),
    owner_id text NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    state text NOT NULL CHECK (
        state IN ('active', 'idle_finalizing', 'idle_expired', 'terminal')
    ),
    last_activity_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

Also create `chat_session_threads`, `chat_session_finalizations`,
`chat_session_owner_claims`, `chat_session_migrations` and
`chat_session_migration_conflicts` using these exact contracts:

```sql
CREATE TABLE IF NOT EXISTS chat_session_threads (
    context_id uuid NOT NULL REFERENCES chat_session_contexts(context_id)
        ON DELETE CASCADE,
    thread_id text NOT NULL,
    adoption_status text NOT NULL DEFAULT 'pending'
        CHECK (adoption_status IN ('pending', 'copying', 'complete')),
    legacy_owner_type text
        CHECK (legacy_owner_type IS NULL OR legacy_owner_type IN ('guest', 'member')),
    legacy_owner_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (context_id, thread_id)
);

CREATE TABLE IF NOT EXISTS chat_session_finalizations (
    finalization_id uuid PRIMARY KEY,
    context_id uuid NOT NULL REFERENCES chat_session_contexts(context_id)
        ON DELETE CASCADE,
    generation bigint NOT NULL,
    reason text NOT NULL CHECK (reason IN ('idle', 'terminal')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'superseded')),
    claim_token text,
    lease_expires_at timestamptz,
    watermark_status text NOT NULL DEFAULT 'pending'
        CHECK (watermark_status IN ('pending', 'captured', 'skipped')),
    profile_watermark bigint CHECK (profile_watermark IS NULL OR profile_watermark >= 0),
    transient_status text NOT NULL DEFAULT 'pending'
        CHECK (transient_status IN ('pending', 'completed')),
    profile_status text NOT NULL DEFAULT 'pending'
        CHECK (profile_status IN (
            'pending', 'processing', 'completed', 'skipped', 'retryable'
        )),
    supersedes_finalization_id uuid REFERENCES chat_session_finalizations(finalization_id)
        ON DELETE SET NULL,
    superseded_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (context_id, generation, reason)
);

CREATE TABLE IF NOT EXISTS chat_session_owner_claims (
    claim_id uuid PRIMARY KEY,
    context_id uuid NOT NULL REFERENCES chat_session_contexts(context_id)
        ON DELETE CASCADE,
    session_id text NOT NULL UNIQUE,
    from_owner_type text NOT NULL CHECK (from_owner_type = 'guest'),
    from_owner_id text NOT NULL,
    to_owner_type text NOT NULL CHECK (to_owner_type = 'member'),
    to_owner_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_session_migrations (
    migration_name text PRIMARY KEY,
    rollout_started_at timestamptz NOT NULL,
    grace_deadline timestamptz NOT NULL,
    profile_backfill_completed_at timestamptz,
    thread_hint_completed_at timestamptz,
    filters_deleted bigint NOT NULL DEFAULT 0,
    cart_deleted bigint NOT NULL DEFAULT 0,
    revert_deleted bigint NOT NULL DEFAULT 0,
    gc_completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_session_migration_conflicts (
    conflict_id bigserial PRIMARY KEY,
    session_id text NOT NULL,
    owner_id text NOT NULL,
    legacy_status text NOT NULL,
    legacy_last_activity_at timestamptz NOT NULL,
    resolution_status text NOT NULL DEFAULT 'quarantined'
        CHECK (resolution_status IN ('quarantined', 'resolved', 'discarded')),
    resolved_context_id uuid REFERENCES chat_session_contexts(context_id)
        ON DELETE SET NULL,
    profile_buffer_discarded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, owner_id)
);
```

Add `idx_chat_session_contexts_due` on
`(state, last_activity_at, context_id)`,
`idx_chat_session_finalizations_lease` on `(lease_expires_at, finalization_id)` where
`status='processing'`, and `idx_chat_session_migration_conflicts_status` on
`(resolution_status, created_at)`.

In `01_conversation_turns.sql`, add nullable `context_id` and `session_id` **without**
the FK because file `01` runs before file `03`. After creating
`chat_session_contexts` in `03_chat_session_contexts.sql`, add the FK:

```sql
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'conversation_turns_context_fk'
          AND conrelid = 'conversation_turns'::regclass
    ) THEN
        ALTER TABLE conversation_turns
            ADD CONSTRAINT conversation_turns_context_fk
            FOREIGN KEY (context_id)
            REFERENCES chat_session_contexts(context_id)
            ON DELETE SET NULL;
    END IF;
END
$$;
```

`initialize_session_lifecycle()` must also `ADD COLUMN IF NOT EXISTS` for both columns
and execute the same guarded constraint creation for existing volumes.

- [ ] **Step 4: Implement PostgreSQL and in-memory behavior**

Use a same-connection transaction-scoped advisory lock:

```sql
SELECT pg_advisory_xact_lock(
    hashtextextended('chat-session:' || %s, 0)
);
```

Use PostgreSQL `now()` for activity/lease decisions and bounded
`FOR UPDATE SKIP LOCKED` for idle claims. The fallback uses one `asyncio.Lock` per
session and a monotonic clock.

Implement these exact public signatures/result types:

- `resolve_touch_register_on_connection(conn, input: BuyerSessionInput) ->
  SessionContext`
- `claim_owner(session_id: str, guest_id: str, user_id: int) -> ClaimOutcome`
- `claim_expired_contexts(idle_timeout_s: float, lease_s: float, batch_size: int) ->
  list[FinalizationClaim]`
- `claim_recoverable_finalizations(lease_s: float, batch_size: int) ->
  list[FinalizationClaim]`
- `list_recoverable_profile_phases(batch_size: int) ->
  list[ProfileRecoveryCandidate]`
- `claim_profile_phase(finalization_id: str, lease_s: float) ->
  FinalizationClaim | None`
- `begin_terminal(user_id: int, session_id: str) -> TerminalOutcome`
- `validate_for_delete(claim: FinalizationClaim) -> SessionContext`
- `mark_idle_finalizing(claim: FinalizationClaim) -> None`
- `complete_transient_phase(claim: FinalizationClaim) -> None`
- `record_profile_phase(finalization_id: str, status: ProfilePhaseStatus) -> None`
- `get_finalization(finalization_id: str) -> SessionFinalization`
- `lock_session(session_id: str) -> AsyncContextManager[SessionContextUnitOfWork]`

`SessionContextUnitOfWork` owns one profile-DB connection, transaction and
`pg_advisory_xact_lock` for its whole context. It exposes
`prepare_idle_finalizing(claim)`, `validate_idle_delete(claim)` and
`complete_idle_delete(claim)` `_on_connection()` operations. Do not implement Phase A
and Phase B as unrelated repository calls that silently acquire different locks.

`claim_recoverable_finalizations()` atomically selects rows where transient work is
pending, finalization status is not `superseded`, the context is `idle_finalizing` or
`terminal`, and the lease is null/expired. Its token/lease replacement CAS repeats the
non-superseded predicate. It uses `FOR UPDATE SKIP LOCKED` and returns the new claim
including `reason: Literal["idle", "terminal"]`.
`claim_expired_contexts()` only creates first-time idle work from `active` contexts
older than the cutoff.

`list_recoverable_profile_phases()` returns member rows whose transient phase is
complete, profile phase is `pending|retryable`, finalization is not superseded, and
watermark status/value are `captured`/non-null; it does not mutate a lease. After the
coordinator reserves the process-local context task slot, `claim_profile_phase()`
CAS-updates only when all of those predicates still hold. This order prevents an
expired DB lease from preempting a still-live local task or a stale idle candidate
from running after I-20 supersession.

Repository methods raise domain errors only. `app/core/errors.py` owns domain-to-HTTP
conversion so generic repository failures still produce the existing safe 500.

- [ ] **Step 5: Add real PostgreSQL concurrency tests**

Assert two concurrent first messages converge on one context, touch invalidates an
idle claim, owner claim is atomic with history, and schema setup is idempotent. Cover
both a brand-new DB init order and a pre-#187 `conversation_turns` table upgraded by
`initialize_session_lifecycle()`.

- [ ] **Step 6: Run repository tests**

```bash
uv run pytest -q tests/unit/test_session_context.py \
  tests/integration/test_pg_session_context.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add db/profile/init/01_conversation_turns.sql \
  db/profile/init/03_chat_session_contexts.sql app/core/session_context.py \
  app/core/config.py app/core/errors.py tests/unit/test_session_context.py \
  tests/integration/test_pg_session_context.py tests/unit/test_infra.py
git commit -m "$(cat <<'EOF'
Give every buyer session one durable lifecycle authority

Constraint: guest and member sessions share expiry without sharing profile semantics
Rejected: profile_session_activity reuse | it excludes guests and couples TTL to profile LLM work
Confidence: high
Scope-risk: broad
Directive: preserve global session uniqueness and generation checks in every mutation
Tested: fallback, PostgreSQL schema, uniqueness, claims, and concurrency tests
Not-tested: buyer graph integration
EOF
)"
```

---

### Task 3: Commit buyer turns through the lifecycle transaction

**Files:**
- Modify: `app/core/conversation.py`
- Modify: `app/core/observability.py`
- Modify: `app/core/stream.py`
- Modify: `app/api/chat.py`
- Modify: `app/api/seller.py`
- Test: `tests/unit/test_observability.py`
- Test: `tests/integration/test_pg_conversation_store.py`
- Test: `tests/unit/test_seller_api.py`

**Interfaces:**
- Consumes: `BuyerSessionInput`, repository touch/register.
- Produces:

```python
@dataclass(frozen=True)
class CommittedTurn:
    turn_id: str
    context_id: str | None
```

- Produces: `RequestObservation.context_id: str | None`.

- [ ] **Step 1: Change tests to require `CommittedTurn`**

Add buyer cases expecting a non-null context, seller cases expecting `None`, and a
rollback test proving a failed turn insert leaves no touched context/thread.

- [ ] **Step 2: Run focused tests and confirm signature failures**

```bash
uv run pytest -q tests/unit/test_observability.py \
  tests/integration/test_pg_conversation_store.py \
  tests/unit/test_seller_api.py
```

- [ ] **Step 3: Update the shared protocol without forcing seller lifecycle**

Exact signature: `save_user_message(self, conversation_id: str, user_id: str | None,
role: str, text: str, *, thread_id: str | None = None, buyer_session:
BuyerSessionInput | None = None) -> CommittedTurn`.

The implementation must return `context_id=None` when `buyer_session is None`.
Buyer context touch/thread registration and turn insert use the same profile-DB
transaction. Stop calling `profile_session_activity.touch_on_connection()`.

- [ ] **Step 4: Wire observation and stream cleanup**

`RequestObservation.commit_user_message()` stores `committed.turn_id` and
`committed.context_id`. `open_stream()` must release the registry slot when commit
raises `SessionForbidden`, `SessionFinalizing` or another exception.

- [ ] **Step 5: Wire buyer only**

After `require_buyer_session()`, construct:

```python
BuyerSessionInput(
    session_id=request.session_id,
    thread_id=request.thread_id,
    owner_type="guest" if identity.is_guest else "member",
    owner_id=buyer_owner_id(identity, get_settings()),
)
```

Seller continues passing no buyer session.

- [ ] **Step 6: Run focused tests**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/core/conversation.py app/core/observability.py app/core/stream.py \
  app/api/chat.py app/api/seller.py tests/unit/test_observability.py \
  tests/integration/test_pg_conversation_store.py tests/unit/test_seller_api.py
git commit -m "$(cat <<'EOF'
Make accepted buyer turns establish session activity atomically

Constraint: seller uses the same conversation store without buyer lifecycle
Confidence: high
Scope-risk: broad
Directive: registry acquisition must stay before commit and registry release must cover commit failure
Tested: observation, PostgreSQL conversation, rollback, and seller regression tests
Not-tested: transient buyer-state migration
EOF
)"
```

---

### Task 4: Move buyer state to v2 context/thread namespaces

**Files:**
- Modify: `app/agents/buyer/graph.py`
- Modify: `app/agents/buyer/cart/state.py`
- Modify: `app/agents/buyer/recommendation/state.py`
- Modify: `app/core/pg_resilience.py`
- Create: `app/agents/buyer/session_state.py`
- Test: `tests/unit/test_cart.py`
- Test: `tests/unit/test_session_context_adoption.py`
- Test: `tests/integration/test_buyer_thread_store.py`

**Interfaces:**
- Consumes: `RequestObservation.context_id`.
- Produces:

```python
def context_thread_key(context_id: str, thread_id: str) -> str:
    return f"{context_id}:{thread_id}"
```

- `clear_context(context_id: str, thread_ids: Sequence[str]) -> CleanupCounts`
- `adopt_legacy_thread(context: SessionContext, thread_id: str, legacy_owner_id:
  str) -> AdoptionResult`
- `ensure_thread_adopted(context_id: str, thread_id: str, current_owner_id: str) ->
  AdoptionResult`

- [ ] **Step 1: Add failing clear/adoption/cache tests**

Assert `clear_thread()` removes filter, pending, last recommendation, one local name
entry and revert categories. Assert scalar new values win, revert categories union,
legacy deletion happens last, and failure leaves adoption incomplete. Add a buyer-flow
test proving adoption runs after the accepted turn commit but before the first filter,
cart or revert v2 read.

- [ ] **Step 2: Add `BoundedLRUCache.pop()`**

```python
def pop(self, key: _K) -> _V | None:
    return self._data.pop(key, None)
```

Test that another cache key remains.

- [ ] **Step 3: Introduce versioned roots**

Use exactly:

```python
_NAMESPACE_ROOT = "buyer_thread_filters_v2"
_NAMESPACE_ROOT = "buyer_cart_v2"
_NAMESPACE_ROOT = "buyer_revert_v2"
```

Keep old root constants in `session_state.py` for read/adopt/GC only.

- [ ] **Step 4: Switch the graph key**

Require `observer.context_id`; derive `context_thread_key()` and remove
`conversation_key(subject, request.thread_id)` from buyer transient state access.
At the top of `run_buyer_turn()`, before `ThreadFilterStore.get()`, call:

```python
await ensure_thread_adopted(
    observer.context_id,
    request.thread_id,
    buyer_owner_id(identity, get_settings()),
)
```

The helper resolves a pre-claim guest legacy owner from owner-claim history when the
current owner is a member.

- [ ] **Step 5: Implement verified adoption and idempotent cleanup**

For scalar values, keep v2 when present. For revert values, write the sorted union.
Only after every v2 read-back matches the intended result, delete legacy persistent
keys, pop the legacy local cache key, and mark adoption complete.

On any adoption read/write/verification failure, raise `SessionStateUnavailable`.
Do not return an empty `AdoptionResult` and do not continue the recommendation graph.
Because the failure occurs on the first async-generator advance, before the first SSE
frame, the centralized error mapping returns 503 `STATE_UNAVAILABLE`; a retry resumes
from the durable non-complete adoption state.

- [ ] **Step 6: Run tests**

```bash
uv run pytest -q tests/unit/test_cart.py \
  tests/unit/test_session_context_adoption.py \
  tests/integration/test_buyer_thread_store.py
```

- [ ] **Step 7: Commit**

```bash
git add app/agents/buyer/graph.py app/agents/buyer/cart/state.py \
  app/agents/buyer/recommendation/state.py app/agents/buyer/session_state.py \
  app/core/pg_resilience.py tests/unit/test_cart.py \
  tests/unit/test_session_context_adoption.py \
  tests/integration/test_buyer_thread_store.py
git commit -m "$(cat <<'EOF'
Keep structured buyer context stable while authentication ownership changes

Constraint: rollout adoption must survive a failure between namespaces
Rejected: copying state during login | it races active streams and multiplies claim work
Confidence: high
Scope-risk: broad
Directive: current writes use only v2 roots; old roots are migration inputs
Tested: store clear, merge, retry, local cache, and integration tests
Not-tested: lifecycle-triggered whole-session cleanup
EOF
)"
```

---

### Task 5: Add the idempotent guest-to-member claim API

**Files:**
- Modify: `app/schemas/events.py`
- Modify: `app/api/events.py`
- Modify: `app/core/session_lifecycle.py`
- Test: `tests/unit/test_session_claim_api.py`

**Interfaces:**
- Consumes: lifecycle repository, active stream registry, service-token dependency.
- Produces:

```python
class SessionClaimEvent(CamelModel):
    session_id: str
    guest_id: str
    user_id: int = Field(gt=0)
```

Coordinator signature: `claim_owner(event: SessionClaimEvent) -> ClaimOutcome`.

- [ ] **Step 1: Add API-contract tests**

Cover accepted, duplicate, active stream, wrong source/target, no-row claim,
`idle_finalizing`, terminal and invalid service token/body.

- [ ] **Step 2: Run tests and confirm the route is missing**

```bash
uv run pytest -q tests/unit/test_session_claim_api.py
```

- [ ] **Step 3: Implement exact transition order**

Under the session advisory lock:

1. Return exact completed claim as no-mutation duplicate.
2. Reject `idle_finalizing` with 409 `SESSION_FINALIZING`.
3. Reject terminal/different target with 409 `SESSION_CLAIM_CONFLICT`.
4. Reject any old-owner active thread with 409 `SESSION_ACTIVE`.
5. Update owner and insert claim history atomically.
6. For no-row claim, create member context plus guest-source history atomically.

- [ ] **Step 4: Add fingerprinted lifecycle logs**

Log context id, generation and outcome. Hash external session/guest identifiers with
the existing peppered HMAC helper. Never log body message, JWT or internal token.

- [ ] **Step 5: Run the route tests**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/events.py app/api/events.py app/core/session_lifecycle.py \
  tests/unit/test_session_claim_api.py
git commit -m "$(cat <<'EOF'
Let login transfer one whole session without moving thread state

Constraint: active or partially deleting guest sessions cannot change owner
Confidence: high
Scope-risk: moderate
Directive: exact duplicate claims never mutate lifecycle state
Tested: claim API authorization, transition, conflict, and idempotency tests
Not-tested: backend retry integration
EOF
)"
```

---

### Task 6: Implement generation-safe session cleanup

**Files:**
- Modify: `app/core/session_lifecycle.py`
- Modify: `app/agents/buyer/session_state.py`
- Test: `tests/unit/test_session_context_cleanup.py`
- Test: `tests/integration/test_pg_session_context.py`

**Interfaces:**
- Produces:

- `process_transient_claim(claim: FinalizationClaim) -> FinalizationOutcome`
- `process_idle_transient(claim: FinalizationClaim) -> FinalizationOutcome`
- `process_terminal_transient(claim: FinalizationClaim) -> FinalizationOutcome`
- `run_session_context_sweep() -> IdleSweepResult`

- [ ] **Step 1: Write the race/failure tests first**

Include touch versus claim, active stream, delete-one-namespace failure, chat/owner
claim during `idle_finalizing`, lease recovery, retry completion, and another context
remaining untouched. Simulate a process crash immediately after the first namespace
delete, recreate repository/coordinator objects, and prove the committed
`idle_finalizing` gate still returns 409. Advance the DB lease past expiry, invoke the
public `run_session_context_sweep()`, and prove it replaces the token and completes
Phase B before chat can reactivate. Repeat the crash/restart scenario for a terminal
finalization and assert it remains terminal. Add partial idle -> I-20 supersede/transfer
-> lease expiry -> public sweep, and assert only the terminal row is reclaimed while
the superseded idle row consumes no recovery batch slot.

- [ ] **Step 2: Run the tests and confirm failure**

```bash
uv run pytest -q tests/unit/test_session_context_cleanup.py \
  tests/integration/test_pg_session_context.py
```

- [ ] **Step 3: Commit the irreversible-delete gate before deleting**

Phase A:

1. Revalidate claim token, lease, generation, state and every registered stream.
2. Set `idle_finalizing`.
3. Capture member profile watermark.
4. Commit and release the transaction-scoped advisory lock.

Phase B:

1. Reacquire the same session-key advisory lock in a new
   `SessionContextUnitOfWork`.
2. Revalidate committed `idle_finalizing`, token, lease and generation.
3. Delete all v2 state idempotently through BaseStore's separate connections.
4. Mark transient completed and context `idle_expired`.
5. Remove runtime thread rows only after completion evidence is stored.
6. Commit Phase B.

Phase A must be durable before the first delete. Leave `idle_finalizing` on Phase B
partial failure or process crash. Chat and new owner claim return retryable 409 until
retry completes.

- [ ] **Step 4: Recover crashed Phase B work before claiming new idle work**

`run_session_context_sweep()` first calls
`claim_recoverable_finalizations(lease_s, batch_size)` and runs Phase B for those
claims. It then spends only the remaining batch capacity on
`claim_expired_contexts(idle_timeout_s, lease_s, remaining)`. Recovery claims replace
expired tokens atomically and never repeat Phase A. Dispatch on `claim.reason`:

- `idle` runs `process_idle_transient()` and may transition to `idle_expired`;
- `terminal` runs `process_terminal_transient()`, completes the transient phase and
  keeps context state `terminal`.

Assert recovery claims consume batch capacity before new idle claims. Emit separate
counts for recovered, superseded-skipped and invalid recovery rows.

- [ ] **Step 5: Run cleanup/concurrency tests**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/session_lifecycle.py app/agents/buyer/session_state.py \
  tests/unit/test_session_context_cleanup.py \
  tests/integration/test_pg_session_context.py
git commit -m "$(cat <<'EOF'
Prevent session expiry from exposing or deleting mixed generations

Constraint: BaseStore deletion and lifecycle rows use separate pooled connections
Confidence: high
Scope-risk: broad
Directive: set idle_finalizing before the first delete and block all new writers until completion
Tested: partial failure, lease recovery, active stream, owner claim, and generation races
Not-tested: profile LLM phase
EOF
)"
```

---

### Task 7: Make profile processing a lifecycle phase

**Files:**
- Modify: `app/agents/profile/finalizer.py`
- Modify: `app/agents/profile/store.py`
- Modify: `app/agents/profile/builder.py`
- Modify: `app/agents/profile/processed_events.py`
- Modify: `app/agents/profile/idle_timeout.py`
- Modify: `app/core/session_lifecycle.py`
- Modify: `app/api/events.py`
- Modify: `app/schemas/events.py`
- Test: `tests/unit/test_profile.py`
- Test: `tests/unit/test_profile_idle_timeout.py`
- Test: `tests/unit/test_session_claim_api.py`
- Test: `tests/integration/test_profile_flow_e2e.py`

**Interfaces:**
- Produces:

Exact signature: `process_profile_checkpoint(user_id: int, session_id: str, *,
event_id: str, profile_watermark: int, settings: Settings) ->
ProfilePhaseResult`.
- Produces: `ActiveProfileTaskRegistry.join_or_start(context_id: str,
  finalization_id: str, event_id: str, factory:
  Callable[[], Awaitable[ProfilePhaseResult]]) -> ProfileJoinResult`
- `ProfileJoinResult` contains `finalization_id`, `event_id`, `result` and `joined`.
- Produces: `process_recoverable_profile_phases(batch_size: int) ->
  list[ProfilePhaseResult]`
- Produces: `ProfileStore.get_session_ctx_upto(key: str, watermark: int) -> list[str]`
- Produces: `generate_session_delta(user_id: str, thread_key: str, *,
  profile_watermark: int, llm: LLMClient | None, settings: Settings) ->
  tuple[list[str], int] | None`
- Produces: `/events/session-end` delegation to
  `SessionLifecycleCoordinator.begin_terminal()`.

- Consumes generation keys:
  `chat-profile:{context_id}:{generation}:{idle|terminal}`.

- [ ] **Step 1: Add profile phase tests**

Assert captured watermark preserves newer entries, repeated idle generations use
different event ids, duplicate completion succeeds, LLM failure is retryable, and
transient cleanup remains completed. Explicitly assert a message with sequence greater
than the watermark is absent from the LLM input and cannot produce a promoted fact.

- [ ] **Step 2: Add the long-LLM I-20 race test**

Block the idle LLM longer than its DB lease, inject I-20, and assert the maximum live
processor count for the context is exactly one. Also run `idle generation 1 profile
task -> new activity -> idle generation 2`; generation 2 may join generation 1 but
must revalidate and run its own event rather than recording generation 1's result.

- [ ] **Step 3: Run focused profile tests and confirm failure**

```bash
uv run pytest -q tests/unit/test_profile.py \
  tests/unit/test_profile_idle_timeout.py \
  tests/unit/test_session_claim_api.py \
  tests/integration/test_profile_flow_e2e.py
```

- [ ] **Step 4: Extract profile work from activity lifecycle**

Remove `ActivityClaim` completion/release from the new processor. Keep
`processed_events` only as generation-scoped phase idempotency. Clear the buffer only
through the supplied profile watermark.

Implement the bounded read exactly:

```python
async def get_session_ctx_upto(self, key: str, watermark: int) -> list[str]:
    item = await run_with_query_timeout(
        self._store.aget((_SESSION_NS_ROOT, key), _SESSION_KEY)
    )
    if not item:
        return []
    return [text for seq, text in item.value["items"] if seq <= watermark]
```

`generate_session_delta()` must call this method when a watermark is supplied; it may
not call `get_session_ctx_snapshot()` again. Return the supplied watermark with the
promotion result.

When the bounded read is empty because cap trimming removed every sequence at or below
the watermark, return a successful `NO_WORK` phase without calling the LLM and without
clearing entries above the watermark. Reserve `RETRYABLE` for a non-empty bounded
buffer that cannot be processed.

Implement `join_or_start()` in this task, after `ProfilePhaseResult` and
`ProfileJoinResult` are defined. Store the created task plus finalization/event
identity before yielding control, await an existing context task, and remove the exact
task in `finally`. Record a result only when returned identity equals the caller's;
otherwise re-read the caller's journal and loop if its phase is still pending.

Implement public profile recovery after transient/new-idle dispatch:

1. List `transient=completed`, `profile=pending|retryable` candidates.
2. Atomically reserve/join the local context registry slot.
3. If this caller owns the slot, call `claim_profile_phase()`; a lost CAS means
   re-read/skip.
4. Run the generation/reason-scoped event and record only the matching result.
5. If another live task owned the slot, join it, revalidate this candidate, and loop
   only when its own phase remains recoverable.

Add DB-time tests for both idle and terminal profile retry rows and prove a live local
task is never lease-preempted. Add `candidate listed -> I-20 supersedes idle row ->
stale CAS returns None -> terminal event alone runs`.

- [ ] **Step 5: Implement terminal supersession**

I-20 marks terminal immediately, blocks new chat/claims, waits for existing streams,
captures watermark afterward, and supersedes/inherits any idle finalization. A live
idle profile task is joined; expired lease alone never starts another local task.

Replace the current direct `finalize_profile_session()` call in
`app/api/events.py::session_end()` with
`SessionLifecycleCoordinator.begin_terminal(user_id, session_id)`. Preserve service
token validation and the 202 accepted/duplicate response contract. Add an endpoint
test proving terminal state is established even when the profile phase is retryable.

- [ ] **Step 6: Run focused tests**

Run the Step 3 command. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/agents/profile/finalizer.py app/agents/profile/store.py \
  app/agents/profile/builder.py app/agents/profile/processed_events.py \
  app/agents/profile/idle_timeout.py app/core/session_lifecycle.py \
  app/api/events.py app/schemas/events.py tests/unit/test_profile.py \
  tests/unit/test_profile_idle_timeout.py tests/unit/test_session_claim_api.py \
  tests/integration/test_profile_flow_e2e.py
git commit -m "$(cat <<'EOF'
Keep transient expiry available while profile consolidation retries safely

Constraint: profile LLM work may outlive a database lease
Confidence: high
Scope-risk: broad
Directive: terminal work joins a live idle profile task and uses generation-scoped event ids
Tested: watermark, duplicate, retry, long-LLM, and terminal supersession tests
Not-tested: scheduler startup migration
EOF
)"
```

---

### Task 8: Backfill safely and replace the scheduler authority

**Files:**
- Modify: `app/core/session_context.py`
- Modify: `app/agents/buyer/session_state.py`
- Modify: `app/pipelines/scheduler.py`
- Modify: `app/main.py`
- Modify: `app/core/config.py`
- Test: `tests/unit/test_scheduler.py`
- Test: `tests/integration/test_pg_session_context.py`

**Interfaces:**
- Produces: `await initialize_session_lifecycle()`.
- Produces: `run_session_context_sweep()` and `run_legacy_gc_batch()`.

- [ ] **Step 1: Write startup/backfill/GC tests**

Cover legacy activity mapping:

- active/processing → active, without carrying lease;
- completed + completed fixed session-end event → terminal;
- other completed → idle-expired;
- duplicate pre-contract session ids → quarantine, not startup failure;
- signed authoritative owner release;
- non-authoritative profile buffer/activity discard after 24 hours;
- v2 state untouched by old-root GC.

- [ ] **Step 2: Make lifespan initialization precede scheduler start**

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await initialize_session_lifecycle()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        await close_advisory_pool()
```

- [ ] **Step 3: Replace the old sweep job**

Register one bounded session-context sweep using the existing interval, batch,
concurrency and lease settings. Do not run the old `profile_session_activity` sweep in
parallel.

- [ ] **Step 4: Implement restart-safe old-root GC**

After the recorded grace deadline, repeatedly fetch the first bounded page from each
legacy namespace root, delete those items, and update durable counts. Mark a root
complete only when a fresh query returns empty.

- [ ] **Step 5: Run startup/scheduler/integration tests**

```bash
uv run pytest -q tests/unit/test_scheduler.py \
  tests/integration/test_pg_session_context.py
```

- [ ] **Step 6: Commit**

```bash
git add app/core/session_context.py app/agents/buyer/session_state.py \
  app/pipelines/scheduler.py app/main.py app/core/config.py \
  tests/unit/test_scheduler.py tests/integration/test_pg_session_context.py
git commit -m "$(cat <<'EOF'
Complete lifecycle migration before any expiry worker can run

Constraint: pre-contract session IDs may collide across owners
Confidence: high
Scope-risk: broad
Directive: quarantine ambiguous owners; never pick one during backfill
Tested: startup ordering, state mapping, collision quarantine, scheduler, and legacy GC
Not-tested: browser three-tab flow
EOF
)"
```

---

### Task 9: Publish the decision and close end-to-end verification

**Files:**
- Create: `docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md`
- Modify: `docs/api-spec.md`
- Modify: `tests/integration/test_buyer_flow_e2e.py`
- Modify: `tests/integration/test_profile_flow_e2e.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces acceptance-to-test traceability and deployment checklist.

- [ ] **Step 1: Write the spec from the approved PRD**

Include the decision, drivers, rejected alternatives, signed-ticket prerequisite,
tables/states, claim contract, idle/I-20 transitions, rollout collision policy,
single-worker invariant, observability and I-21 follow-up.

- [ ] **Step 2: Add D6 and guest-to-member E2E**

Automate:

1. G1 creates S1/T1-T3.
2. Touch T1 and prove all three survive before the shared threshold.
3. Expire S1 and prove all structured state clears together.
4. Rebuild, claim to M1, continue all three with the same context ids.
5. Prove old G1 receives 403 and creates no branch.
6. Prove transcripts remain and guest turns are absent from durable M1 facts.

- [ ] **Step 3: Run targeted suites**

```bash
uv run pytest -q tests/unit/test_auth_e2e.py \
  tests/unit/test_session_claim_api.py \
  tests/unit/test_session_context.py \
  tests/unit/test_session_context_cleanup.py \
  tests/unit/test_session_context_adoption.py
uv run pytest -q tests/integration/test_pg_session_context.py \
  tests/integration/test_pg_conversation_store.py \
  tests/integration/test_buyer_thread_store.py \
  tests/integration/test_buyer_flow_e2e.py \
  tests/integration/test_profile_flow_e2e.py
```

- [ ] **Step 4: Run repository-wide verification**

```bash
uv run pytest -q
uv run ruff check
git diff --check
```

Expected: all commands exit 0. Record if no typecheck command exists.

- [ ] **Step 5: Perform the cross-repository release gate**

Before production:

1. The BE #63 release owner records evidence that production tickets contain signed
   `sessionId` and that `ticketTtlSeconds` is 60.
2. The AI release owner waits 90 seconds after the last pre-contract ticket could be
   minted: 60-second TTL plus 30-second safety margin.
3. Deploy AI enforcement.
4. Run FE #52 three-tab login/refresh flow.
5. Verify missing-session ticket errors, claim conflicts and cleanup retries are zero
   or explained.

- [ ] **Step 6: Commit**

```bash
git add docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md docs/api-spec.md \
  tests/integration/test_buyer_flow_e2e.py \
  tests/integration/test_profile_flow_e2e.py
git commit -m "$(cat <<'EOF'
Make the session-lifecycle decision auditable before release

Constraint: full login continuity spans AI, backend, and frontend repositories
Confidence: high
Scope-risk: moderate
Directive: do not mark #187 shipped until signed-ticket and three-tab release gates pass
Tested: targeted lifecycle suites, full pytest, Ruff, and diff check
Not-tested: production traffic
EOF
)"
```

---

## ADR

**Decision:** Use a signed `sessionId`, globally unique stable `context_id`, dedicated
chat lifecycle tables and phase-journaled cleanup.

**Drivers:** strict old-owner denial; guest/member D6 parity; retry-safe cleanup and
rollout.

**Alternatives considered:** owner-scoped context without a ticket change;
generalizing `profile_session_activity`; deriving threads from audit rows and adding
item TTL.

**Why chosen:** Only the selected design prevents stale guest recreation, avoids
login-time state copying, expires all threads together and keeps transient TTL
independent of profile LLM availability.

**Consequences:** BE ticket rollout is a hard prerequisite; AI gains a schema and
migration subsystem; single-worker remains an explicit invariant; ambiguous legacy
session collisions are quarantined rather than guessed.

**Follow-ups:** BE #63, FE #52, I-21 retention, distributed stream/profile fencing
before horizontal scale, and a separate privacy review of existing chat logs.

## Execution dependency graph

```text
Task 1 auth contract ─┐
                     ├─> Task 3 conversation commit ─> Task 5 owner claim
Task 2 repository ───┘                 │
                                      ├─> Task 6 cleanup ─> Task 7 profile phase
Task 4 buyer state ────────────────────┘                     │
                                                            ├─> Task 8 rollout/scheduler
                                                            └─> Task 9 docs/E2E
```

Tasks 1, 2 and the store-local portion of Task 4 can run in parallel with strict file
ownership. Tasks 3 and 4 must be integrated before Task 5/6. Tasks 6 and 7 are
sequential because they share `session_lifecycle.py`.

## Agent roster and staffing guidance

Available relevant agent types:

- `executor` — implementation owner for auth, repository, state and coordinator.
- `test-engineer` — concurrency/fault/E2E tests.
- `architect` — state-machine and migration review.
- `code-reviewer` — security, async cancellation and transaction review.
- `verifier` — fresh completion evidence.
- `explore` — bounded symbol/file lookups.
- `dependency-expert` — only if psycopg/LangGraph behavior needs an external package
  decision; no new dependency is planned.

Recommended Team staffing:

- Executor A: Task 1 only.
- Executor B: Task 2 only.
- Executor C: store-local Task 4 files only.
- Leader/executor: Tasks 3, 5, 6, 7 and integration of shared files.
- Test engineer: adversarial matrices after Task 6, without owning implementation
  files.
- Code reviewer then verifier: final sequential gates.

Suggested reasoning levels: medium for bounded executor tasks; high for
test-engineer/code-reviewer/verifier; xhigh for any reopened lifecycle architecture
decision.

## Team launch and verification path

For coordinated execution, use `$team` only after this consensus handoff:

```text
$team implement docs/superpowers/plans/2026-07-30-issue-187-session-context-lifecycle.md
```

Team verification sequence:

1. Each executor runs its focused tests before handoff.
2. Leader runs integration tests after every dependency-wave merge.
3. Test engineer runs race/fault matrix.
4. Code reviewer checks auth binding, transaction/lock order, cancellation and
   migration rollback.
5. Verifier independently runs full pytest, Ruff and diff check and maps evidence to
   #187 acceptance.

## Goal-Mode Follow-up Suggestions

- **Recommended:** `$ultragoal` for durable task-by-task delivery, optionally combined
  with `$team` for Tasks 1/2/4 parallelism.
- `$team` alone when coordinated parallel implementation is more important than a
  durable goal ledger.
- `$ralph` only as an explicit fallback for a single-owner persistence loop after the
  approved plan exists.
- `$autoresearch-goal` is not appropriate; this is implementation, not open-ended
  research.
- `$performance-goal` is not appropriate unless later work measures sweep/lock
  throughput.
