BEGIN;

ALTER TABLE donation ALTER COLUMN fee DROP NOT NULL;

COMMIT;
