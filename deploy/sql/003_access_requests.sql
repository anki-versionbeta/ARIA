-- 003_access_requests.sql
--
-- Somebody with no modules asks for the ones they need, and any admin approves it in the
-- app. This replaces a mailto: link, which left no record of who asked for what and put
-- the request in a mailbox rather than in front of whoever was looking at the tool.
--
-- One pending request per person, enforced by a partial unique index rather than by
-- application code: the check and the write would otherwise race, and two browser tabs is
-- all it takes. Re-requesting updates the row in place, so changing your mind is not a
-- second queue entry.
--
-- Decided requests are kept. "Who asked for mfg_atr, when, and who approved it" is the
-- question this table exists to answer six months from now; deleting the row on approval
-- would answer only "they have it".

BEGIN;

CREATE TABLE IF NOT EXISTS access_requests (
    id            varchar(36) PRIMARY KEY,
    user_id       varchar(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The module ids asked for, as a JSON array. A join table would be tidier in the
    -- abstract, but this is a request rather than state -- it is written once, read as a
    -- whole, and never joined against.
    modules       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    note          text,
    status        text        NOT NULL DEFAULT 'pending',
    created_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz,
    decided_by    varchar(128),
    decision_note text
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'access_requests_status_check') THEN
        ALTER TABLE access_requests
            ADD CONSTRAINT access_requests_status_check
            CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn'));
    END IF;
END $$;

-- At most one open request per person.
CREATE UNIQUE INDEX IF NOT EXISTS ux_access_requests_one_pending
    ON access_requests (user_id)
    WHERE status = 'pending';

-- The admin queue reads pending-first, oldest-first.
CREATE INDEX IF NOT EXISTS ix_access_requests_status_created
    ON access_requests (status, created_at);

COMMIT;

-- Verify:
--   SELECT r.status, u.username, r.modules, r.created_at
--     FROM access_requests r JOIN users u ON u.id = r.user_id ORDER BY r.created_at DESC;
--
-- To roll back:
--   DROP TABLE IF EXISTS access_requests;
