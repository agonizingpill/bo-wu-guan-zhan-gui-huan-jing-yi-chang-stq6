-- 同厅展柜异常归并候选、认领处置单与可恢复的派单意图。
-- 一条告警（anomaly_events.sample_id）只对应一个异常事件。

ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS escalation_basis VARCHAR(40);

-- 历史数据若因并发评分产生同一采样的重复告警，保留最早一条后建立唯一约束。
DELETE FROM anomaly_events a USING anomaly_events b
WHERE a.sample_id = b.sample_id AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_anomaly_events_sample ON anomaly_events(sample_id);

CREATE TABLE IF NOT EXISTS anomaly_candidates (
  id SERIAL PRIMARY KEY,
  gallery VARCHAR(120) NOT NULL,
  signature CHAR(64) UNIQUE NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','superseded')),
  revision INTEGER NOT NULL DEFAULT 1,
  score_min NUMERIC(8,5) NOT NULL,
  score_max NUMERIC(8,5) NOT NULL,
  window_start TIMESTAMPTZ NOT NULL,
  window_end TIMESTAMPTZ NOT NULL,
  direction VARCHAR(20) NOT NULL,
  member_count INTEGER NOT NULL DEFAULT 0,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_anomaly_candidates_gallery
  ON anomaly_candidates(gallery, status, window_start DESC);

-- event_id 主键保证同一告警最多被一个候选占用。
CREATE TABLE IF NOT EXISTS anomaly_candidate_members (
  event_id INTEGER PRIMARY KEY REFERENCES anomaly_events(id) ON DELETE CASCADE,
  candidate_id INTEGER NOT NULL REFERENCES anomaly_candidates(id) ON DELETE CASCADE,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_anomaly_candidate_members_candidate
  ON anomaly_candidate_members(candidate_id);

CREATE TABLE IF NOT EXISTS handling_tickets (
  id SERIAL PRIMARY KEY,
  dispatch_key VARCHAR(120) UNIQUE NOT NULL,
  candidate_id INTEGER REFERENCES anomaly_candidates(id) ON DELETE SET NULL,
  candidate_revision INTEGER NOT NULL,
  gallery VARCHAR(120) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed','in_progress','closed','cancelled')),
  claimed_by INTEGER NOT NULL REFERENCES users(id),
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  member_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_handling_tickets_candidate ON handling_tickets(candidate_id);
CREATE INDEX IF NOT EXISTS idx_handling_tickets_gallery ON handling_tickets(gallery, claimed_at DESC);

-- event_id 唯一：并发评分与并发认领时，同一告警只能进入一张处置单。
CREATE TABLE IF NOT EXISTS handling_ticket_members (
  ticket_id INTEGER NOT NULL REFERENCES handling_tickets(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL UNIQUE REFERENCES anomaly_events(id) ON DELETE CASCADE,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  PRIMARY KEY (ticket_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_handling_ticket_members_case ON handling_ticket_members(case_id);

-- 派单意图先落库，再在第二个事务中成单；中断后重试或恢复可继续，dispatch_key 防止重复开单。
CREATE TABLE IF NOT EXISTS dispatch_attempts (
  dispatch_key VARCHAR(120) PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES anomaly_candidates(id) ON DELETE CASCADE,
  candidate_revision INTEGER NOT NULL,
  state VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','dispatching','completed','failed')),
  attempts INTEGER NOT NULL DEFAULT 1,
  ticket_id INTEGER REFERENCES handling_tickets(id) ON DELETE SET NULL,
  actor_id INTEGER REFERENCES users(id),
  note VARCHAR(500) NOT NULL DEFAULT '',
  snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_open
  ON dispatch_attempts(state, updated_at) WHERE state IN ('pending','dispatching');

-- 兼容已存在的旧表结构。
ALTER TABLE dispatch_attempts ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
