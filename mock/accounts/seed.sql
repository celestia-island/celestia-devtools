-- scripts/mock/seed.sql — PostgreSQL seed data for the dev mock stack.
--
-- Injected by db::seed_mock() AFTER migrations but BEFORE the backend serves.
-- Gives the panel pre-seeded users + workspaces + RBAC so a dev can log in
-- immediately and see a diverse, multi-user / multi-workspace mock.
--
-- Password for every account: 33550336 (argon2id, m=19456 t=2 p=1 — the Rust
-- default). The hash below is shared; the verify path only needs a valid hash.

-- ── Users ────────────────────────────────────────────────────────────
--   demiurge — admin. The local_auth endpoint (mock-mode) looks this
--              username up directly so the Tauri desktop client auto-logs
--              into it without credentials. Email uses the unified
--              @celestia.world domain (local fleet vs xxx@celestia.world
--              public cloud).
--   momoi   — operator, owns the host filesystem workspace
--   midori  — operator, owns the remote-git (entelecheia) workspace
--   yuzu    — operator, owns the temporary container workspace
INSERT INTO auth_users (id, username, email, password_hash, display_name, is_active, is_admin, role, tier, relay_id, created_at, updated_at)
VALUES
    ('00000000-0000-4000-8000-000000000001', 'demiurge', 'demiurge@celestia.world', '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'demiurge', true, true,  'admin',   'standard', '00000000-0000-4000-8000-000000000001', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000101', 'momoi',    'momoi@celestia.world',    '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'momoi',    true, false, 'operator','standard', '00000000-0000-4000-8000-000000000101', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000102', 'midori',   'midori@celestia.world',   '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'midori',   true, false, 'operator','standard', '00000000-0000-4000-8000-000000000102', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000103', 'yuzu',     'yuzu@celestia.world',     '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'yuzu',     true, false, 'operator','standard', '00000000-0000-4000-8000-000000000103', NOW(), NOW())
ON CONFLICT (username) DO NOTHING;

-- ── System settings ─────────────────────────────────────────────────
INSERT INTO system_settings (key, value, created_at, updated_at)
VALUES
    ('proxy_ws_enabled', 'true'::jsonb, NOW(), NOW()),
    ('registration_enabled', 'false'::jsonb, NOW(), NOW())
ON CONFLICT (key) DO NOTHING;

-- ── Personal workspace (admin) ──────────────────────────────────────
-- UUID matches the frontend's DEFAULT_WORKSPACE_ID (router/index.ts) so the
-- first login lands directly on the admin's personal workspace. ensure_personal_workspace()
-- in the backend finds it by (user_id, kind='personal') and reuses it.
INSERT INTO workspace_sessions (id, user_id, workspace_path, editor_name, editor_version, capabilities, status, connected_at, connection_kind, kind)
VALUES (
    '3a7bc1d2-e4f5-6789-0abc-def012345678',
    '00000000-0000-4000-8000-000000000001',
    '',
    'personal',
    '',
    '["read","write","admin"]'::jsonb,
    'active',
    NOW(),
    'personal',
    'personal'
)
ON CONFLICT DO NOTHING;

-- ── Three shared default workspaces ──────────────────────────────────
--   #1 host         — local_filesystem, a real host path            (owner: momoi)
--   #2 git          — remote git checkout of entelecheia (depth=1)  (owner: midori)
--   #3 container    — temporary empty container scratch space       (owner: yuzu)
-- The admin (al1s) is granted viewer access on all three.
-- (The git workspace_path is a placeholder; a mock-mode hook in api.rs
--  rewrites it at startup to the entelecheia checkout resolved from
--  ENTELECHEIA_ROOT / the Cargo.toml [patch] path.)
INSERT INTO workspace_sessions (id, user_id, workspace_path, editor_name, editor_version, git_remote_url, git_branch, capabilities, status, connected_at, connection_kind, kind)
VALUES
    ('00000000-0000-4000-8000-000000000201',
     '00000000-0000-4000-8000-000000000101',
     '/mnt/sdb1/shittim-chest',
     'local_filesystem', 'dev',
     NULL, NULL,
     '["read","write"]'::jsonb,
     'active', NOW(),
     'local_filesystem', 'local_filesystem'),
    ('00000000-0000-4000-8000-000000000202',
     '00000000-0000-4000-8000-000000000102',
     '/mnt/sdb1/entelecheia',
     'git', 'dev',
     'https://github.com/celestia-island/entelecheia.git',
     'master',
     '["read","write"]'::jsonb,
     'active', NOW(),
     'git', 'git'),
    ('00000000-0000-4000-8000-000000000203',
     '00000000-0000-4000-8000-000000000103',
     '/tmp/mock-container',
     'container', 'dev',
     NULL, NULL,
     '["read","write"]'::jsonb,
     'active', NOW(),
     'container', 'container')
ON CONFLICT DO NOTHING;

-- ── RBAC: workspace membership ──────────────────────────────────────
-- one owner per workspace (partial unique idx), plus the admin as viewer on all three.
INSERT INTO workspace_members (id, workspace_id, user_id, role, granted_by, created_at, updated_at)
VALUES
    ('00000000-0000-4000-8000-000000000301', '00000000-0000-4000-8000-000000000201', '00000000-0000-4000-8000-000000000101', 'owner',  '00000000-0000-4000-8000-000000000001', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000302', '00000000-0000-4000-8000-000000000202', '00000000-0000-4000-8000-000000000102', 'owner',  '00000000-0000-4000-8000-000000000001', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000303', '00000000-0000-4000-8000-000000000203', '00000000-0000-4000-8000-000000000103', 'owner',  '00000000-0000-4000-8000-000000000001', NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000311', '00000000-0000-4000-8000-000000000201', '00000000-0000-4000-8000-000000000001', 'viewer', NULL, NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000312', '00000000-0000-4000-8000-000000000202', '00000000-0000-4000-8000-000000000001', 'viewer', NULL, NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000313', '00000000-0000-4000-8000-000000000203', '00000000-0000-4000-8000-000000000001', 'viewer', NULL, NOW(), NOW()),
    -- The frontend's DEFAULT_WORKSPACE_ID (the admin personal workspace) is the
    -- post-login landing page for a fresh browser; grant every operator viewer
    -- access so they can land + open the app, then switch to their own workspace.
    ('00000000-0000-4000-8000-000000000321', '3a7bc1d2-e4f5-6789-0abc-def012345678', '00000000-0000-4000-8000-000000000101', 'viewer', NULL, NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000322', '3a7bc1d2-e4f5-6789-0abc-def012345678', '00000000-0000-4000-8000-000000000102', 'viewer', NULL, NOW(), NOW()),
    ('00000000-0000-4000-8000-000000000323', '3a7bc1d2-e4f5-6789-0abc-def012345678', '00000000-0000-4000-8000-000000000103', 'viewer', NULL, NOW(), NOW())
ON CONFLICT DO NOTHING;
