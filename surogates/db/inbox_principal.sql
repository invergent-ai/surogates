-- Allow inbox items to belong to a service account instead of a user.
-- Service-account-owned sessions (e.g. ops Studio chats) have no user_id, so
-- without this their inbox items were never created (the creation guard in
-- store.emit_event skipped them). Idempotent: safe to run on every startup.
ALTER TABLE inbox_items ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE inbox_items
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id);
