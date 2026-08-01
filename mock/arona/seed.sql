-- celestia-devtools/mock/arona/seed.sql
-- Mock user seed for arona's PostgreSQL schema.
-- Password for every account: 33550336
-- Injected by arona when MOCK_MODE=1 and MOCK_SEED_PATH is set.

INSERT INTO users (email, password_hash, display_name) VALUES
    ('demiurge@celestia.world', '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'demiurge'),
    ('momoi@celestia.world',    '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'momoi'),
    ('midori@celestia.world',   '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'midori'),
    ('yuzu@celestia.world',     '$argon2id$v=19$m=19456,t=2,p=1$g+R2E9nM4il7uhe1lEwYaQ$Qedvga5n1MioMwkVFdEYf84FWoS3aAEH0Pl9P5q8rD8', 'yuzu')
ON CONFLICT (email) DO NOTHING;
