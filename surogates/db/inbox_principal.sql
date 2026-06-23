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
