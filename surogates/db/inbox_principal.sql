-- Allow inbox items to belong to a service account instead of a user.
-- Service-account-owned sessions (e.g. ops Studio chats) have no user_id, so
-- without this their inbox items were never created (the creation guard in
-- store.emit_event skipped them). Idempotent: safe to run on every startup.
ALTER TABLE inbox_items ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE inbox_items
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id);

-- Exactly one principal (user OR service account), matching the sessions,
-- scheduled_sessions and browser_profiles tables. No CHECK ... IF NOT EXISTS
-- in older Postgres, so guard against re-adding it.
DO $$ BEGIN
    ALTER TABLE inbox_items
        ADD CONSTRAINT ck_inbox_items_one_principal
        CHECK ((user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Index the service-account inbox query (ops scopes by service_account_id +
-- status, ordered by created_at), mirroring idx_inbox_user_status_created.
CREATE INDEX IF NOT EXISTS idx_inbox_sa_status_created
    ON inbox_items (service_account_id, status, created_at);

-- The expiry sweeper's working set. Every other index here is led by a
-- principal, so its "pending items, by kind and age" pass had to read the
-- whole table every 300s — on a table that only ever grows, since inbox rows
-- are kept as history. Partial on status because pending is the small,
-- shrinking minority of rows, which also keeps the write cost near zero:
-- an entry is added when an item is created and removed when it is resolved.
CREATE INDEX IF NOT EXISTS idx_inbox_pending_kind_created
    ON inbox_items (kind, created_at)
    WHERE status = 'pending';
