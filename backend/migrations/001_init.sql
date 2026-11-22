CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username VARCHAR(60) UNIQUE NOT NULL,
  display_name VARCHAR(120) NOT NULL,
  role VARCHAR(20) NOT NULL CHECK (role IN ('operator','supervisor','viewer')),
  password_hash VARCHAR(128) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS display_cases (
  id SERIAL PRIMARY KEY,
  code VARCHAR(40) UNIQUE NOT NULL,
  gallery VARCHAR(120) NOT NULL,
  zone VARCHAR(120) NOT NULL,
  artifact_code VARCHAR(60) NOT NULL,
  artifact_title VARCHAR(180) NOT NULL,
  target_temp_min NUMERIC(5,2) NOT NULL,
  target_temp_max NUMERIC(5,2) NOT NULL,
  target_rh_min NUMERIC(5,2) NOT NULL,
  target_rh_max NUMERIC(5,2) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','maintenance','retired')),
  door_sensor_id VARCHAR(60) NOT NULL,
  last_sample_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (target_temp_min < target_temp_max),
  CHECK (target_rh_min < target_rh_max)
);

CREATE TABLE IF NOT EXISTS environment_samples (
  id SERIAL PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  sampled_at TIMESTAMPTZ NOT NULL,
  temperature NUMERIC(5,2) NOT NULL,
  humidity NUMERIC(5,2) NOT NULL,
  co2_ppm NUMERIC(7,2) NOT NULL,
  door_open BOOLEAN NOT NULL DEFAULT FALSE,
  source VARCHAR(30) NOT NULL DEFAULT 'manual',
  state VARCHAR(20) NOT NULL DEFAULT 'accepted' CHECK (state IN ('accepted','rejected')),
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(case_id, sampled_at)
);

CREATE TABLE IF NOT EXISTS anomaly_events (
  id SERIAL PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
  algorithm_run_id INTEGER,
  score NUMERIC(8,5) NOT NULL,
  threshold NUMERIC(8,5) NOT NULL,
  anomaly_type VARCHAR(40) NOT NULL CHECK (anomaly_type IN ('environment_outlier','door_interference','continuous_drift')),
  severity VARCHAR(20) NOT NULL CHECK (severity IN ('watch','high','critical')),
  status VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open','triaged','escalated','resolved','closed')),
  door_context BOOLEAN NOT NULL DEFAULT FALSE,
  consecutive_count INTEGER NOT NULL DEFAULT 1,
  escalated_to_curator BOOLEAN NOT NULL DEFAULT FALSE,
  reason TEXT NOT NULL,
  recovery_note TEXT,
  created_by INTEGER REFERENCES users(id),
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS duty_actions (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES anomaly_events(id) ON DELETE CASCADE,
  action_type VARCHAR(40) NOT NULL CHECK (action_type IN ('acknowledge','verify_door_sealed','adjust_climate','inspect_sensor','call_curator')),
  status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','completed','rejected')),
  notes TEXT NOT NULL,
  operator_id INTEGER NOT NULL REFERENCES users(id),
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS artifact_checks (
  id SERIAL PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  event_id INTEGER REFERENCES anomaly_events(id) ON DELETE SET NULL,
  check_type VARCHAR(40) NOT NULL CHECK (check_type IN ('routine_round','post_incident')),
  result VARCHAR(20) NOT NULL CHECK (result IN ('pending','pass','fail')),
  notes TEXT NOT NULL,
  inspector_id INTEGER NOT NULL REFERENCES users(id),
  checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS algorithm_runs (
  id SERIAL PRIMARY KEY,
  run_key VARCHAR(120) UNIQUE NOT NULL,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  status VARCHAR(20) NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id SERIAL PRIMARY KEY,
  actor_id INTEGER REFERENCES users(id),
  action VARCHAR(80) NOT NULL,
  entity_type VARCHAR(80) NOT NULL,
  entity_id INTEGER,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_samples_case_time ON environment_samples(case_id, sampled_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_status ON anomaly_events(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at DESC);
