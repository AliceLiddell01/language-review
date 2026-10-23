CREATE TABLE lr_job_attempts (
    attempt_id uuid PRIMARY KEY,
    job_id text NOT NULL REFERENCES lr_jobs(id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence > 0),
    owner uuid NOT NULL,
    stage text NOT NULL CHECK (stage IN (
        'claim','context','probe','completion','parse_schema','post_schema','persistence','indexing'
    )),
    outcome text NOT NULL CHECK (outcome IN (
        'running','succeeded','failed','stale','interrupted'
    )),
    disposition text NOT NULL CHECK (disposition IN (
        'running','retry','failed_final','completed','stale','interrupted'
    )),
    error text CHECK (error IS NULL OR (length(error) <= 80 AND error ~ '^[a-z0-9_]+$')),
    subreason text CHECK (
        subreason IS NULL OR subreason IN (
            'evidence_alias_unknown','evidence_outside_permitted','current_evidence_missing',
            'category_mismatch','sanitize_changed','empty_text_after_strip','json_invalid',
            'duplicate_json_key','schema_validation_failed'
        )
    ),
    stages jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(stages) = 'object' AND length(stages::text) <= 8192
    ),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE(job_id, sequence),
    CHECK ((outcome = 'running' AND disposition = 'running') OR
           (outcome = 'succeeded' AND disposition = 'completed') OR
           (outcome = 'failed' AND disposition IN ('retry','failed_final')) OR
           (outcome = 'stale' AND disposition = 'stale') OR
           (outcome = 'interrupted' AND disposition IN ('interrupted','failed_final','retry'))),
    CHECK ((outcome = 'running' AND finished_at IS NULL) OR
           (outcome <> 'running' AND finished_at IS NOT NULL))
);
CREATE INDEX lr_job_attempts_job_sequence ON lr_job_attempts(job_id, sequence DESC);
