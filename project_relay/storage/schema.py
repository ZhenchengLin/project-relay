SCHEMA_VERSION = 4

MIGRATION_1_SQL = r'''
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repository_root TEXT NOT NULL UNIQUE,
    expected_branch TEXT,
    conversation_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL
        REFERENCES projects(id)
        ON DELETE CASCADE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE requests (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
        REFERENCES sessions(id)
        ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL
        CHECK (sequence_number >= 1),
    state TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    user_turn_id TEXT,
    assistant_turn_id TEXT,
    assistant_text_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_number)
);

CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE
        REFERENCES requests(id)
        ON DELETE CASCADE,
    command_sha256 TEXT NOT NULL,
    cwd TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    return_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    terminal_output_sha256 TEXT,
    git_before_json TEXT,
    git_after_json TEXT
);

CREATE TABLE watchdog_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL
        REFERENCES requests(id)
        ON DELETE CASCADE,
    voter TEXT NOT NULL,
    model TEXT,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE watchdog_decisions (
    request_id TEXT PRIMARY KEY
        REFERENCES requests(id)
        ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL
        REFERENCES projects(id)
        ON DELETE CASCADE,
    session_id TEXT
        REFERENCES sessions(id)
        ON DELETE CASCADE,
    request_id TEXT
        REFERENCES requests(id)
        ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX events_project_id_id
    ON events(project_id, id);

CREATE INDEX events_session_id_id
    ON events(session_id, id);

CREATE INDEX events_request_id_id
    ON events(request_id, id);

CREATE TABLE browser_leases (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
        REFERENCES sessions(id)
        ON DELETE CASCADE,
    conversation_url TEXT NOT NULL,
    browser_surface_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE UNIQUE INDEX one_active_browser_lease_per_session
    ON browser_leases(session_id)
    WHERE status = 'active';
'''


# V2: extension-driven relay.
#
# conversations   one project owns a lineage of ChatGPT conversations;
#                 rollover retires the active one and starts a successor.
# project_runtime one autonomous run per project (status, model mode,
#                 cycle budget).
MIGRATION_2_SQL = r'''
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL
        REFERENCES projects(id)
        ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL
        CHECK (sequence_number >= 1),
    conversation_url TEXT,
    predecessor_id TEXT
        REFERENCES conversations(id),
    status TEXT NOT NULL,
    retire_reason TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    UNIQUE(project_id, sequence_number)
);

CREATE UNIQUE INDEX one_active_conversation_per_project
    ON conversations(project_id)
    WHERE status = 'ACTIVE';

CREATE UNIQUE INDEX one_project_per_active_conversation_url
    ON conversations(conversation_url)
    WHERE status = 'ACTIVE'
      AND conversation_url IS NOT NULL;

ALTER TABLE requests ADD COLUMN conversation_id TEXT
    REFERENCES conversations(id);
ALTER TABLE requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'CYCLE';
ALTER TABLE requests ADD COLUMN model TEXT;
ALTER TABLE requests ADD COLUMN baseline_json TEXT;
ALTER TABLE requests ADD COLUMN assistant_text TEXT;
ALTER TABLE requests ADD COLUMN detail TEXT;
ALTER TABLE requests ADD COLUMN successor_request_id TEXT;
ALTER TABLE requests ADD COLUMN browser_lease TEXT;

ALTER TABLE executions ADD COLUMN command_text TEXT;
ALTER TABLE executions ADD COLUMN pid INTEGER;

CREATE TABLE project_runtime (
    project_id TEXT PRIMARY KEY
        REFERENCES projects(id)
        ON DELETE CASCADE,
    session_id TEXT
        REFERENCES sessions(id),
    status TEXT NOT NULL,
    reason TEXT,
    model_mode TEXT NOT NULL DEFAULT 'DEFAULT',
    progress_streak INTEGER NOT NULL DEFAULT 0,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    max_cycles INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
'''


# V3: Relay ADE. A project run is either `solo` (one chat plans and writes
# commands) or `ade` (a PM chat plans/reviews, a Worker chat writes commands).
# Conversations and requests carry the role they belong to; each role has its
# own active conversation and rollover lineage.
MIGRATION_3_SQL = r'''
ALTER TABLE conversations ADD COLUMN role TEXT NOT NULL DEFAULT 'worker';
ALTER TABLE conversations ADD COLUMN site TEXT NOT NULL DEFAULT 'chatgpt';
ALTER TABLE requests ADD COLUMN role TEXT NOT NULL DEFAULT 'worker';
ALTER TABLE project_runtime ADD COLUMN mode TEXT NOT NULL DEFAULT 'solo';
ALTER TABLE project_runtime ADD COLUMN goal TEXT;
ALTER TABLE project_runtime ADD COLUMN review_policy TEXT NOT NULL DEFAULT 'risky';

DROP INDEX one_active_conversation_per_project;

CREATE UNIQUE INDEX one_active_conversation_per_project_role
    ON conversations(project_id, role)
    WHERE status = 'ACTIVE';
'''


# V4: project memory and the PM's plan.
#
# project_notes  durable facts a project should never forget (decisions,
#                rules, lessons). Written by the PM (RELAY_NOTE:), by you
#                (dashboard / CLI) or by Relay; injected into every kickoff,
#                handoff and fresh chat.
# plan_tasks     the milestone list the PM maintains with a RELAY_PLAN block.
MIGRATION_4_SQL = r'''
CREATE TABLE project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL
        REFERENCES projects(id)
        ON DELETE CASCADE,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    request_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX project_notes_project_active
    ON project_notes(project_id, active);

CREATE TABLE plan_tasks (
    project_id TEXT NOT NULL
        REFERENCES projects(id)
        ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_key)
);
'''
