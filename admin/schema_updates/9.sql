BEGIN;

ALTER TABLE token_log ALTER COLUMN user_id DROP NOT NULL;

COMMIT;
