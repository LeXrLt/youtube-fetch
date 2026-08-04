ALTER TABLE agent_invocations
  DROP CONSTRAINT agent_invocations_status_check,
  ADD CONSTRAINT agent_invocations_status_check CHECK (
    status IN ('succeeded', 'failed', 'cancelled')
  ),
  ADD CONSTRAINT agent_invocations_terminal_fields_check CHECK (
    (status = 'succeeded' AND error_message IS NULL)
    OR (
      status IN ('failed', 'cancelled')
      AND NULLIF(btrim(error_message), '') IS NOT NULL
    )
  ),
  ADD CONSTRAINT agent_invocations_time_order_check CHECK (
    finished_at >= started_at
  );
