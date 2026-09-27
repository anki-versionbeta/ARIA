-- 001_worker_allowlist_guard.sql
--
-- Only approved hosts may claim runs.
--
-- Why this exists: the runs table IS the job queue, so any machine that can reach this
-- database and runs `python -m worker` becomes a live worker. On 2026-08-12 a developer
-- workstation (ULCLS-PF3CHAWR) was doing exactly that against the shared dev database.
-- Its AWS credentials were temporary STS keys, which cannot renew themselves, so every
-- job it happened to claim died with:
--
--     (ExpiredToken) when calling the PutObject operation: The provided token has expired
--
-- while the same job on the EC2 -- which holds an instance role and renews itself --
-- succeeded. Whichever worker won the claim decided the outcome, which is why the
-- failures looked random and why a retry appeared to "fix" it. Application-side guards
-- cannot help: the offending machine runs its own checkout of older code. The database
-- is the one place every worker meets, so the rule belongs here.
--
-- Fails OPEN on purpose: an empty worker_allowlist disables enforcement entirely.
--     DELETE FROM worker_allowlist;      -- complete off switch, no DDL, no restart
--
-- FOOTGUN: if the server is rebuilt with a different private IP, its worker identity
-- (socket.getfqdn() + pid + uuid) no longer matches and claims are rejected, so runs sit
-- in 'queued' forever. Fix by adding the new prefix:
--     INSERT INTO worker_allowlist (host_prefix, note) VALUES ('ip-10-x-x-x', 'new server');
-- Check this table first if runs mysteriously stop being picked up.

CREATE TABLE IF NOT EXISTS worker_allowlist (
    host_prefix text PRIMARY KEY,
    note        text,
    added_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO worker_allowlist (host_prefix, note)
VALUES ('ip-10-224-134-56', 'ARIA dev EC2 - the only approved worker host')
ON CONFLICT (host_prefix) DO NOTHING;

CREATE OR REPLACE FUNCTION reject_unapproved_worker_claim()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    -- Empty allowlist == rule disabled (see the off switch above).
    IF NOT EXISTS (SELECT 1 FROM worker_allowlist) THEN
        RETURN NEW;
    END IF;

    -- Releasing a claim is always allowed: finish() and mark_failed() set NULL.
    IF NEW.claimed_by IS NULL THEN
        RETURN NEW;
    END IF;

    -- Re-writing the same claim (heartbeats, refreshes) is not a new claim.
    IF TG_OP = 'UPDATE' AND OLD.claimed_by IS NOT DISTINCT FROM NEW.claimed_by THEN
        RETURN NEW;
    END IF;

    IF EXISTS (SELECT 1 FROM worker_allowlist w
               WHERE NEW.claimed_by LIKE w.host_prefix || '%') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'ARIA: worker host "%" is not approved to claim runs. Approved host prefixes live in the worker_allowlist table; run the worker on the ARIA server instead.',
        NEW.claimed_by
        USING ERRCODE = 'insufficient_privilege';
END;
$fn$;

DROP TRIGGER IF EXISTS trg_reject_unapproved_worker_claim ON runs;
CREATE TRIGGER trg_reject_unapproved_worker_claim
BEFORE INSERT OR UPDATE OF claimed_by ON runs
FOR EACH ROW EXECUTE FUNCTION reject_unapproved_worker_claim();

-- To remove the rule completely:
--   DROP TRIGGER IF EXISTS trg_reject_unapproved_worker_claim ON runs;
--   DROP FUNCTION IF EXISTS reject_unapproved_worker_claim();
--   DROP TABLE IF EXISTS worker_allowlist;
