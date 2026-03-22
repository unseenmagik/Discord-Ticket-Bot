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
    assignee_discord_user_id BIGINT NULL,
    assignee_display_name VARCHAR(255) NULL,
    assigned_at VARCHAR(64) NULL,
    assigned_by_discord_user_id BIGINT NULL,
    assigned_by_display_name VARCHAR(255) NULL,
    INDEX idx_status (status),
    INDEX idx_opener_server_status (opener_id, server_label, status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key VARCHAR(100) PRIMARY KEY,
    setting_value TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dashboard_audit_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    actor_discord_user_id BIGINT NOT NULL,
    actor_username VARCHAR(255) NOT NULL,
    actor_display_name VARCHAR(255) NOT NULL,
    ticket_thread_id BIGINT NULL,
    metadata_json TEXT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_dashboard_audit_created_at (created_at),
    INDEX idx_dashboard_audit_actor (actor_discord_user_id),
    INDEX idx_dashboard_audit_event_type (event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_internal_notes (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    author_discord_user_id BIGINT NOT NULL,
    author_display_name VARCHAR(255) NOT NULL,
    note_text TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_ticket_internal_notes_thread_created (thread_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_tags (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tag_key VARCHAR(100) NOT NULL UNIQUE,
    tag_name VARCHAR(100) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    created_by_discord_user_id BIGINT NULL,
    created_by_display_name VARCHAR(255) NULL,
    INDEX idx_ticket_tags_name (tag_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_tag_assignments (
    ticket_thread_id BIGINT NOT NULL,
    tag_id BIGINT NOT NULL,
    assigned_at VARCHAR(64) NOT NULL,
    assigned_by_discord_user_id BIGINT NULL,
    assigned_by_display_name VARCHAR(255) NULL,
    PRIMARY KEY (ticket_thread_id, tag_id),
    INDEX idx_ticket_tag_assignments_tag_id (tag_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_thread_notices (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    color INT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    processed_at VARCHAR(64) NULL,
    INDEX idx_ticket_thread_notices_processed_created (processed_at, created_at),
    INDEX idx_ticket_thread_notices_thread (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_thread_member_sync (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    action VARCHAR(16) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    processed_at VARCHAR(64) NULL,
    INDEX idx_ticket_thread_member_sync_processed_created (processed_at, created_at),
    INDEX idx_ticket_thread_member_sync_thread (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
