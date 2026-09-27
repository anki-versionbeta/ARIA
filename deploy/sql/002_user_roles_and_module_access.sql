-- 002_user_roles_and_module_access.sql
--
-- Role-based module access.
--
-- Three roles. `admin` and `super_user` reach every module by definition, so no rows are
-- written for them and none are read; only `user` is gated by user_module_access. That is
-- why granting a module to a super user is a no-op rather than an error -- the role
-- already implies everything the grant would say.
--
-- A "module" is a silo id (bop, iso, mfg_atr). ATR and MFGR are report types *inside*
-- mfg_atr, not separate silos, so they are granted together as one module. If they ever
-- need separating, that is a schema change here, not a config tweak.
--
-- Reverses two earlier decisions on purpose: D12 (every silo visible to every
-- authenticated user) and D13 (any user may read any document). Both now depend on the
-- caller holding the module.
--
-- Grandfathering: every user that already existed keeps access to everything, so nobody
-- loses access the moment this deploys. Users created after this migration start with no
-- modules and see an empty state telling them to contact an admin.

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'user';

-- Cheap guard against a typo writing a meaningless role.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check') THEN
        ALTER TABLE users
            ADD CONSTRAINT users_role_check
            CHECK (role IN ('admin', 'super_user', 'user'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS user_module_access (
    user_id    varchar(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id  varchar(64) NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by varchar(128),
    PRIMARY KEY (user_id, module_id)
);

CREATE INDEX IF NOT EXISTS ix_user_module_access_user ON user_module_access (user_id);

-- Repair, not decoration: with DA_ENV=local the app calls SQLAlchemy create_all() at
-- import, so this table may already have been created from the model, where granted_at
-- carries a Python-side default and the generated DDL therefore has NOT NULL and no
-- DEFAULT. Any INSERT that omits the column then fails. Set it explicitly so the outcome
-- is the same whichever created the table first.
ALTER TABLE user_module_access ALTER COLUMN granted_at SET DEFAULT now();

-- Grandfather everyone who already exists (see note above). granted_at is passed
-- explicitly as well, so this does not depend on the default above having applied.
INSERT INTO user_module_access (user_id, module_id, granted_at, granted_by)
SELECT u.id, m.module_id, now(), 'migration-002'
FROM users u
CROSS JOIN (VALUES ('bop'), ('iso'), ('mfg_atr')) AS m(module_id)
WHERE u.first_seen < now()
ON CONFLICT (user_id, module_id) DO NOTHING;

-- Bootstrap admins. Without this nobody can reach user management and the feature is
-- inert, so it belongs in the migration rather than in a runbook step someone forgets.
UPDATE users SET role = 'admin' WHERE username IN ('bapatar', 'mohanax25');

COMMIT;

-- Verify:
--   SELECT username, role FROM users ORDER BY role, username;
--   SELECT u.username, count(a.module_id) FROM users u
--     LEFT JOIN user_module_access a ON a.user_id = u.id GROUP BY 1 ORDER BY 1;
--
-- To roll back completely:
--   DROP TABLE IF EXISTS user_module_access;
--   ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
--   ALTER TABLE users DROP COLUMN IF EXISTS role;
