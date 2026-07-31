CREATE TABLE IF NOT EXISTS chat_session_contexts (
    context_id uuid PRIMARY KEY,
    session_id text NOT NULL UNIQUE,
    owner_type text NOT NULL CHECK (owner_type IN ('guest', 'member')),
    owner_id text NOT NULL,
    authority_source text NOT NULL DEFAULT 'runtime'
        CHECK (authority_source IN ('runtime', 'legacy_backfill')),
    generation bigint NOT NULL DEFAULT 0,
    state text NOT NULL CHECK (
        state IN ('active', 'idle_finalizing', 'idle_expired', 'terminal')
    ),
    last_activity_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

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
    profile_backfill_cursor text,
    profile_backfill_owner_cursor bigint,
    profile_backfill_pass bigint NOT NULL DEFAULT 0,
    conflict_gc_cursor bigint NOT NULL DEFAULT 0,
    legacy_writer_seen_at timestamptz,
    legacy_quiet_until timestamptz,
    legacy_quiet_window_s double precision NOT NULL DEFAULT 90,
    CONSTRAINT chat_session_migrations_legacy_quiet_window_check
        CHECK (legacy_quiet_window_s >= 90),
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

CREATE INDEX IF NOT EXISTS idx_chat_session_contexts_due
    ON chat_session_contexts (state, last_activity_at, context_id);
CREATE INDEX IF NOT EXISTS idx_chat_session_finalizations_lease
    ON chat_session_finalizations (lease_expires_at, finalization_id)
    WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_chat_session_migration_conflicts_status
    ON chat_session_migration_conflicts (resolution_status, created_at);

ALTER TABLE chat_session_contexts ADD COLUMN IF NOT EXISTS authority_source text;
ALTER TABLE chat_session_migrations ADD COLUMN IF NOT EXISTS profile_backfill_cursor text;
ALTER TABLE chat_session_migrations ADD COLUMN IF NOT EXISTS profile_backfill_owner_cursor bigint;
ALTER TABLE chat_session_migrations
    ADD COLUMN IF NOT EXISTS profile_backfill_pass bigint NOT NULL DEFAULT 0;
ALTER TABLE chat_session_migrations
    ADD COLUMN IF NOT EXISTS conflict_gc_cursor bigint NOT NULL DEFAULT 0;
ALTER TABLE chat_session_migrations ADD COLUMN IF NOT EXISTS legacy_writer_seen_at timestamptz;
ALTER TABLE chat_session_migrations ADD COLUMN IF NOT EXISTS legacy_quiet_until timestamptz;
ALTER TABLE chat_session_migrations
    ADD COLUMN IF NOT EXISTS legacy_quiet_window_s double precision NOT NULL DEFAULT 90;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='chat_session_migrations_legacy_quiet_window_check'
          AND conrelid='chat_session_migrations'::regclass
    ) THEN
        ALTER TABLE chat_session_migrations
            ADD CONSTRAINT chat_session_migrations_legacy_quiet_window_check
            CHECK (legacy_quiet_window_s >= 90);
    END IF;
END
$$;
UPDATE chat_session_contexts c
SET authority_source='runtime'
WHERE authority_source IS NULL
  AND (
      EXISTS (SELECT 1 FROM chat_session_threads t WHERE t.context_id=c.context_id)
      OR EXISTS (
          SELECT 1 FROM chat_session_owner_claims h WHERE h.context_id=c.context_id
      )
  );
UPDATE chat_session_contexts
SET authority_source='legacy_backfill'
WHERE authority_source IS NULL;
ALTER TABLE chat_session_contexts
    ALTER COLUMN authority_source SET DEFAULT 'runtime';
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND table_name='chat_session_contexts'
          AND column_name='authority_source'
          AND is_nullable='YES'
    ) THEN
        ALTER TABLE chat_session_contexts
            ALTER COLUMN authority_source SET NOT NULL;
    END IF;
END
$$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='chat_session_contexts_authority_source_check'
          AND conrelid='chat_session_contexts'::regclass
    ) THEN
        ALTER TABLE chat_session_contexts
            ADD CONSTRAINT chat_session_contexts_authority_source_check
            CHECK (authority_source IN ('runtime', 'legacy_backfill'));
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('profile_session_activity') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS idx_profile_session_activity_session_owner
            ON profile_session_activity (session_id, user_id);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION reopen_session_context_gc_after_legacy_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE chat_session_migrations
    SET legacy_writer_seen_at=now(),
        legacy_quiet_until=GREATEST(
            COALESCE(legacy_quiet_until, '-infinity'::timestamptz),
            now() + make_interval(secs => legacy_quiet_window_s)
        ),
        gc_completed_at=NULL,
        updated_at=now()
    WHERE migration_name='issue-187-session-context';
    RETURN NULL;
END
$$;

DO $$
BEGIN
    IF to_regclass('profile_session_activity') IS NOT NULL THEN
        IF NOT EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname='trg_reopen_session_context_gc_after_legacy_activity'
             AND tgrelid=to_regclass('profile_session_activity')
        ) THEN
            CREATE TRIGGER trg_reopen_session_context_gc_after_legacy_activity
            AFTER INSERT OR UPDATE ON profile_session_activity
            FOR EACH STATEMENT
            EXECUTE FUNCTION reopen_session_context_gc_after_legacy_activity();
        END IF;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION install_session_context_legacy_store_trigger()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('store') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_reopen_session_context_gc_after_legacy_store ON store;
        CREATE TRIGGER trg_reopen_session_context_gc_after_legacy_store
        AFTER INSERT OR UPDATE ON store
        FOR EACH ROW
        WHEN (
            split_part(NEW.prefix, '.', 1) IN (
                'buyer_thread_filters',
                'buyer_cart',
                'buyer_revert'
            )
        )
        EXECUTE FUNCTION reopen_session_context_gc_after_legacy_activity();
    END IF;
END
$$;

SELECT install_session_context_legacy_store_trigger();

ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS context_id uuid;
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS session_id text;

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
