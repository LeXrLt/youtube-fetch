CREATE TABLE agent_invocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_run_id uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
  stage text NOT NULL,
  sequence_number integer NOT NULL,
  status text NOT NULL,
  thread_id text,
  agent_input jsonb NOT NULL,
  full_prompt text NOT NULL,
  intermediate_events jsonb NOT NULL DEFAULT '[]'::jsonb,
  final_response text,
  agent_output jsonb,
  usage jsonb,
  error_message text,
  started_at timestamptz NOT NULL,
  finished_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT agent_invocations_stage_not_blank CHECK (btrim(stage) <> ''),
  CONSTRAINT agent_invocations_sequence_positive CHECK (sequence_number > 0),
  CONSTRAINT agent_invocations_status_check CHECK (status IN ('succeeded', 'failed')),
  CONSTRAINT agent_invocations_run_stage_sequence_unique
    UNIQUE (analysis_run_id, stage, sequence_number)
);

CREATE INDEX idx_agent_invocations_run
  ON agent_invocations(analysis_run_id, stage, sequence_number);

CREATE INDEX idx_agent_invocations_thread
  ON agent_invocations(thread_id)
  WHERE thread_id IS NOT NULL;
