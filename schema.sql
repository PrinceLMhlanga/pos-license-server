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

-- sms queue
CREATE TABLE IF NOT EXISTS sms_messages (
  id SERIAL PRIMARY KEY,
  phone TEXT NOT NULL,
  email TEXT,
  message TEXT NOT NULL,
  method TEXT DEFAULT 'sms',
  license_id INT REFERENCES licenses(id),
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INT DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  response_json JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS license_activations (
    id SERIAL PRIMARY KEY,
    license_id INT REFERENCES licenses(id),
    terminal_id TEXT NOT NULL,
    activated_at TIMESTAMPTZ DEFAULT now()
);
