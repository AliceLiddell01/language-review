CREATE INDEX lr_jobs_finding ON lr_jobs(finding_id, revision);
CREATE INDEX lr_rules_finding ON lr_rules(finding_id);
CREATE INDEX lr_rules_evidence ON lr_rules USING gin ((payload->'evidence') jsonb_path_ops);
