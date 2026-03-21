CREATE TABLE IF NOT EXISTS tickets (
    thread_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    opener_id BIGINT NOT NULL,
    opener_name VARCHAR(255) NOT NULL,
    server_label VARCHAR(255) NOT NULL,
    target_channel_id BIGINT NOT NULL,
    seed_message_id BIGINT NOT NULL,
    status ENUM('open', 'closed', 'deleted') NOT NULL DEFAULT 'open',
    created_at VARCHAR(64) NOT NULL,
    closed_at VARCHAR(64) NULL,
    closed_by_id BIGINT NULL,
    closed_by_name VARCHAR(255) NULL,
    deleted_at VARCHAR(64) NULL,
    log_message_id BIGINT NULL,
    transcript_message_url TEXT NULL,
    reopened_at VARCHAR(64) NULL,
    reopened_by_id BIGINT NULL,
    reopened_by_name VARCHAR(255) NULL,
    deleted_by_id BIGINT NULL,
    deleted_by_name VARCHAR(255) NULL,
    INDEX idx_status (status),
    INDEX idx_opener_server_status (opener_id, server_label, status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key VARCHAR(100) PRIMARY KEY,
    setting_value TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
