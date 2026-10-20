CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE lr_projects (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lr_runs (
    id uuid PRIMARY KEY,
    bindings jsonb NOT NULL,
    report_hash text,
    provenance text NOT NULL CHECK (provenance IN ('live','historical')),
    dataset text NOT NULL CHECK (dataset IN ('operational','synthetic','train','dev','test','holdout')),
    status text NOT NULL DEFAULT 'started',
    report jsonb,
    diagnostics jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);
CREATE TABLE lr_documents (
    run_id uuid NOT NULL REFERENCES lr_runs(id),
    id text NOT NULL,
    project_id uuid NOT NULL REFERENCES lr_projects(id),
    payload jsonb NOT NULL,
    PRIMARY KEY(run_id, id)
);
CREATE TABLE lr_findings (
    id text PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES lr_projects(id),
    identity jsonb NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'unverified'
        CHECK (state IN ('unverified','accepted','rejected','disputed')),
    revision integer NOT NULL DEFAULT 0,
    dataset text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lr_occurrences (
    run_id uuid NOT NULL REFERENCES lr_runs(id),
    item_id text NOT NULL,
    finding_id text NOT NULL REFERENCES lr_findings(id),
    payload jsonb NOT NULL,
    PRIMARY KEY(run_id, item_id),
    UNIQUE(run_id, finding_id)
);
CREATE TABLE lr_events (
    id text PRIMARY KEY,
    run_id uuid REFERENCES lr_runs(id),
    finding_id text REFERENCES lr_findings(id),
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lr_decisions (
    id uuid PRIMARY KEY,
    finding_id text NOT NULL REFERENCES lr_findings(id),
    revision integer NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted','rejected','disputed')),
    actor text NOT NULL,
    reason text NOT NULL,
    suggestion integer,
    scope text NOT NULL CHECK (scope IN ('project','global')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(finding_id, revision)
);
CREATE TABLE lr_jobs (
    id text PRIMARY KEY,
    finding_id text NOT NULL REFERENCES lr_findings(id),
    revision integer NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    owner uuid,
    error text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX lr_jobs_ready ON lr_jobs(state, available_at);
CREATE TABLE lr_rules (
    id text PRIMARY KEY,
    project_id uuid REFERENCES lr_projects(id),
    finding_id text NOT NULL REFERENCES lr_findings(id),
    decision_id uuid REFERENCES lr_decisions(id),
    kind text NOT NULL CHECK (kind IN ('case','hypothesis')),
    state text NOT NULL CHECK (state IN ('accepted','rejected','disputed','hypothesis','superseded')),
    revision integer NOT NULL,
    category text NOT NULL,
    context_kind text NOT NULL,
    text text NOT NULL,
    payload jsonb NOT NULL,
    dataset text NOT NULL,
    search tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', text) || to_tsvector('simple', text)
    ) STORED,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX lr_rules_scope ON lr_rules(project_id, state, dataset);
CREATE INDEX lr_rules_fts ON lr_rules USING gin(search);
CREATE TABLE lr_rule_history (
    rule_id text NOT NULL REFERENCES lr_rules(id),
    revision integer NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(rule_id, revision)
);
CREATE TABLE lr_embedding_spaces (
    id text PRIMARY KEY,
    model text NOT NULL,
    revision text NOT NULL,
    dimensions integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lr_embeddings (
    rule_id text NOT NULL REFERENCES lr_rules(id),
    space_id text NOT NULL REFERENCES lr_embedding_spaces(id),
    text_hash text NOT NULL,
    embedding vector NOT NULL,
    PRIMARY KEY(rule_id, space_id)
);
