-- 1. TẠO DATABASE
CREATE DATABASE IF NOT EXISTS api_gateway_db
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE api_gateway_db;

-- 2. TẠO TÀI KHOẢN MYSQL & CẤP QUYỀN
-- (Dùng 'localhost' nếu code Flask và MySQL nằm trên cùng 1 server. Đổi thành '%' nếu nằm ở 2 server khác nhau)
CREATE USER IF NOT EXISTS 'githubcopilot'@'localhost' IDENTIFIED BY 'ghcplserver';
GRANT ALL PRIVILEGES ON api_gateway_db.* TO 'githubcopilot'@'localhost';
FLUSH PRIVILEGES;

-- 3. TẠO CÁC BẢNG (TABLES)

-- Bảng users: Quản lý thông tin tài khoản và API Key
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    api_key VARCHAR(100) UNIQUE NULL DEFAULT NULL,
    role ENUM('user', 'admin') DEFAULT 'user',
    credit DECIMAL(12, 6) DEFAULT 0.000000, -- Số dư (USD) của user, dùng để tính chi phí per token
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Bảng models: Lưu trữ cấu hình và bảng giá của từng model
-- Giá tính theo USD per 1,000 tokens (input riêng, output riêng)
-- Công thức trừ tiền: cost = (prompt_tokens × input_price + completion_tokens × output_price) / 1000
CREATE TABLE IF NOT EXISTS models (
    id INT AUTO_INCREMENT PRIMARY KEY,
    model_id VARCHAR(50) UNIQUE NOT NULL,    -- Ví dụ: claude-sonnet-4.5
    name VARCHAR(100) NOT NULL,              -- Tên hiển thị
    input_price DECIMAL(12, 6) NOT NULL,     -- Giá cho mỗi 1,000 token đầu vào (Prompt)
    output_price DECIMAL(12, 6) NOT NULL,    -- Giá cho mỗi 1,000 token đầu ra (Completion)
    discount_percent DECIMAL(5, 2) DEFAULT 0.00, -- Phần trăm giảm giá (0.00 = không giảm, 50.00 = giảm 50%)
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Bảng usage_history: Lưu vết mọi request để trừ tiền và đối soát
CREATE TABLE IF NOT EXISTS usage_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    model_id VARCHAR(50) NOT NULL,
    prompt_tokens INT NOT NULL,
    completion_tokens INT NOT NULL,
    total_cost DECIMAL(12, 6) NOT NULL,
    ip_address VARCHAR(45), -- Độ dài 45 để chứa được cả địa chỉ IPv6
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Khóa ngoại (Foreign Keys)
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON UPDATE CASCADE
);

-- Bảng payment_orders: Lưu vết mọi giao dịch nạp tiền (PayPal, Sepay, ...)
CREATE TABLE IF NOT EXISTS payment_orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    order_id VARCHAR(100) UNIQUE NOT NULL,       -- PayPal order ID hoặc Sepay transaction ID
    provider ENUM('paypal', 'sepay', 'bank_transfer') NOT NULL,
    amount DECIMAL(12, 6) NOT NULL,              -- Số tiền (USD)
    status ENUM('pending', 'completed', 'failed', 'refunded') DEFAULT 'pending',
    metadata JSON DEFAULT NULL,                  -- Dữ liệu bổ sung từ provider
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- TẠO CHỈ MỤC (INDEX) TỐI ƯU TỐC ĐỘ TRUY VẤN
CREATE INDEX idx_users_api_key ON users(api_key);
CREATE INDEX idx_usage_user_id ON usage_history(user_id);
CREATE INDEX idx_usage_created_at ON usage_history(created_at);
CREATE INDEX idx_payment_order_id ON payment_orders(order_id);
CREATE INDEX idx_payment_user_id ON payment_orders(user_id, created_at DESC);

-- 4. INSERT DỮ LIỆU MẪU: BẢNG GIÁ 24 MODELS
-- =============================================================================
-- Nguồn giá gốc thị trường: Anthropic docs, Google AI, artificialanalysis.ai
-- Quy tắc giá bán OrchestraAPI:
--   Input  = giá gốc thị trường × 2/3  (giảm ~33%)
--   Output = nếu ratio output/input gốc >= 5x → 6× input_bán (giảm ~20%)
--            ngược lại → output gốc × 2/3  (giảm ~33%)
-- Đơn vị: USD per 1,000 tokens
-- Loại bỏ: text-embedding, grok, raptor mini, các variant date (chỉ giữ model gốc)
-- =============================================================================

INSERT INTO models (model_id, name, input_price, output_price, is_active) VALUES
-- ── Claude Models ───────────────────────────────────────────────────────────
-- Opus: gốc $5/$25 per 1M → bán $3.33/$20 per 1M (↓33%/↓20%)
('claude-opus-4.6-fast',   'Claude Opus 4.6 Fast',       0.003333, 0.020000, TRUE),
('claude-opus-4.6',        'Claude Opus 4.6',            0.003333, 0.020000, TRUE),
('claude-opus-4.5',        'Claude Opus 4.5',            0.003333, 0.020000, TRUE),
-- Sonnet: gốc $3/$15 per 1M → bán $2/$12 per 1M (↓33%/↓20%)
('claude-sonnet-4.6',      'Claude Sonnet 4.6',          0.002000, 0.012000, TRUE),
('claude-sonnet-4.5',      'Claude Sonnet 4.5',          0.002000, 0.012000, TRUE),
('claude-sonnet-4',        'Claude Sonnet 4',            0.002000, 0.012000, TRUE),
-- Haiku: gốc $1/$5 per 1M → bán $0.667/$4.00 per 1M (↓33%/↓20%)
('claude-haiku-4.5',       'Claude Haiku 4.5',           0.000667, 0.004000, TRUE),

-- ── Gemini Models ───────────────────────────────────────────────────────────
-- 3.1 Pro / 3 Pro: gốc $2/$12 per 1M → bán $1.333/$8 per 1M (↓33%/↓33%)
('gemini-3.1-pro-preview', 'Gemini 3.1 Pro',             0.001333, 0.008000, TRUE),
('gemini-3-pro-preview',   'Gemini 3 Pro',               0.001333, 0.008000, TRUE),
-- 2.5 Pro: gốc $1.25/$10 per 1M → bán $0.833/$5.00 per 1M (↓33%/↓50%)
('gemini-2.5-pro',         'Gemini 2.5 Pro',             0.000833, 0.005000, TRUE),
-- Flash: gốc $0.50/$3 per 1M → bán $0.333/$2.00 per 1M (↓33%/↓33%)
('gemini-3-flash-preview', 'Gemini 3 Flash',             0.000333, 0.002000, TRUE),

-- ── GPT-5.x Models ─────────────────────────────────────────────────────────
-- Codex/Standard tier: gốc $1.75/$14 per 1M → bán $1.167/$7.00 per 1M (↓33%/↓50%)
('gpt-5.3-codex',          'GPT-5.3 Codex',              0.001167, 0.007000, TRUE),
('gpt-5.2-codex',          'GPT-5.2 Codex',              0.001167, 0.007000, TRUE),
('gpt-5.2',                'GPT-5.2',                    0.001167, 0.007000, TRUE),
('gpt-5.1-codex-max',      'GPT-5.1 Codex Max',          0.001167, 0.007000, TRUE),
('gpt-5.4',                'GPT-5.4',                    0.001167, 0.007000, TRUE),
-- 5.1 tier: gốc $1.25/$10 per 1M → bán $0.833/$5.00 per 1M (↓33%/↓50%)
('gpt-5.1-codex',          'GPT-5.1 Codex',              0.000833, 0.005000, TRUE),
('gpt-5.1',                'GPT-5.1',                    0.000833, 0.005000, TRUE),
-- Mini: gốc $0.25/$2 per 1M → bán $0.167/$1.00 per 1M (↓33%/↓50%)
('gpt-5.1-codex-mini',     'GPT-5.1 Codex Mini',         0.000167, 0.001000, TRUE),
('gpt-5-mini',             'GPT-5 Mini',                 0.000167, 0.001000, TRUE),

-- ── GPT Legacy Models ───────────────────────────────────────────────────────
-- 4o/4.1/4: gốc $0.20/$1.50 per 1M → ratio 7.5x → bán $0.133/$0.80 per 1M (↓33%/↓47%)
('gpt-4o',                 'GPT-4o',                     0.000133, 0.000800, TRUE),
('gpt-4.1',                'GPT-4.1',                    0.000133, 0.000800, TRUE),
('gpt-4o-mini',            'GPT-4o Mini',                0.000100, 0.000400, TRUE),
('gpt-4',                  'GPT-4',                      0.000133, 0.000800, TRUE),
('gpt-3.5-turbo',          'GPT-3.5 Turbo',              0.000333, 0.001000, TRUE)

ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    input_price = VALUES(input_price),
    output_price = VALUES(output_price);

-- 5. MIGRATION: Add discount_percent column (safe for existing databases)
-- Run manually if upgrading from a version without discount support:
--   ALTER TABLE models ADD COLUMN discount_percent DECIMAL(5, 2) DEFAULT 0.00;
