-- orders table
CREATE TABLE IF NOT EXISTS orders (
  id SERIAL PRIMARY KEY,
  provider TEXT NOT NULL,
  provider_order_id TEXT NOT NULL UNIQUE,
  amount_cents INT,
  currency TEXT,
  customer_phone TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT now()
);

-- licenses
CREATE TABLE IF NOT EXISTS licenses (
  id SERIAL PRIMARY KEY,
  license_key TEXT NOT NULL UNIQUE,
  product_sku TEXT,
  order_id INT REFERENCES orders(id),
  issued_to TEXT,
  issued_phone TEXT,
  issued_email TEXT,
  issued_at TIMESTAMPTZ DEFAULT now(),
  expires_at TIMESTAMPTZ,
  status TEXT DEFAULT 'valid',
  activated BOOLEAN DEFAULT FALSE,
  activated_at TIMESTAMPTZ,
  metadata JSONB
);

-- sms/email queue (notifications/messages)
CREATE TABLE IF NOT EXISTS sms_messages (
  id SERIAL PRIMARY KEY,
  phone TEXT,                             -- allow NULL for email-only messages
  email TEXT,
  message TEXT NOT NULL,
  method TEXT DEFAULT 'sms',               -- 'sms' or 'email'
  license_id INT REFERENCES licenses(id),
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | sending | sent | failed
  attempts INT DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ,                    -- timestamp when successfully sent
  response_json JSONB,                    -- provider response or error info
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sms_messages_status ON sms_messages(status);
CREATE INDEX IF NOT EXISTS idx_sms_messages_created_at ON sms_messages(created_at);

CREATE TABLE IF NOT EXISTS license_activations (
  id SERIAL PRIMARY KEY,
  license_id INT REFERENCES licenses(id),
  terminal_id TEXT NOT NULL,
  activated_at TIMESTAMPTZ DEFAULT now()
);
