-- ─────────────────────────────────────────────────────────
-- SmAttaker — Database Initialization Script
-- Run on Supabase / PostgreSQL
-- ─────────────────────────────────────────────────────────

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    telegram_id BIGINT UNIQUE NOT NULL,
    telegram_username VARCHAR(128),
    email VARCHAR(255) UNIQUE,
    full_name VARCHAR(255),
    role VARCHAR(32) DEFAULT 'user' NOT NULL,
    status VARCHAR(32) DEFAULT 'pending_approval' NOT NULL,
    trial_start TIMESTAMPTZ,
    trial_end TIMESTAMPTZ,
    approved_by_admin BOOLEAN DEFAULT FALSE,
    language VARCHAR(8) DEFAULT 'en' NOT NULL,
    default_account_type VARCHAR(16) DEFAULT 'demo' NOT NULL,
    notes VARCHAR(1024),
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_users_telegram_id ON users(telegram_id);
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_email ON users(email);

-- Subscriptions table
CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_type VARCHAR(32) DEFAULT 'monthly' NOT NULL,
    amount_usd FLOAT DEFAULT 99.0 NOT NULL,
    payment_method VARCHAR(32) NOT NULL,
    payment_status VARCHAR(32) DEFAULT 'pending' NOT NULL,
    stripe_subscription_id VARCHAR(255) UNIQUE,
    stripe_payment_intent_id VARCHAR(255),
    crypto_tx_hash VARCHAR(512),
    crypto_currency VARCHAR(32),
    crypto_amount FLOAT,
    start_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_date TIMESTAMPTZ,
    auto_renew BOOLEAN DEFAULT TRUE NOT NULL,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(payment_status);

-- Signals table
CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_type VARCHAR(32) NOT NULL,
    strategy_version VARCHAR(32),
    symbol VARCHAR(32) NOT NULL,
    exchange VARCHAR(64),
    asset_class VARCHAR(32) NOT NULL,
    direction VARCHAR(8) NOT NULL,
    entry_price FLOAT NOT NULL,
    entry_zone_high FLOAT,
    entry_zone_low FLOAT,
    stop_loss FLOAT NOT NULL,
    stop_loss_pct FLOAT DEFAULT 0 NOT NULL,
    risk_reward_ratio FLOAT,
    take_profit_levels JSONB,
    confidence_score FLOAT,
    ml_metadata JSONB,
    technical_snapshot JSONB,
    status VARCHAR(32) DEFAULT 'active' NOT NULL,
    expiry_minutes INTEGER DEFAULT 60 NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    broadcast_count INTEGER DEFAULT 0 NOT NULL,
    executed_trades_count INTEGER DEFAULT 0 NOT NULL,
    outcome VARCHAR(32),
    outcome_price FLOAT,
    outcome_pnl_pct FLOAT,
    is_premium_only BOOLEAN DEFAULT FALSE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_signals_type ON signals(strategy_type);
CREATE INDEX idx_signals_symbol ON signals(symbol);
CREATE INDEX idx_signals_status ON signals(status);

-- Trades table
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_id UUID REFERENCES signals(id) ON DELETE SET NULL,
    account_type VARCHAR(16) DEFAULT 'demo' NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    exchange VARCHAR(64),
    strategy VARCHAR(64) NOT NULL,
    asset_class VARCHAR(32) NOT NULL,
    direction VARCHAR(8) NOT NULL,
    order_type VARCHAR(32) DEFAULT 'market' NOT NULL,
    entry_price FLOAT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    entry_order_id VARCHAR(255),
    stop_loss FLOAT NOT NULL,
    stop_loss_pct FLOAT DEFAULT 0 NOT NULL,
    trailing_stop BOOLEAN DEFAULT FALSE,
    trailing_distance_pct FLOAT,
    take_profit_levels JSONB,
    position_size FLOAT NOT NULL,
    position_size_usd FLOAT,
    leverage INTEGER DEFAULT 1 NOT NULL,
    risk_percent FLOAT DEFAULT 0 NOT NULL,
    risk_amount_usd FLOAT,
    exit_price FLOAT,
    exit_time TIMESTAMPTZ,
    exit_reason VARCHAR(32),
    exit_order_id VARCHAR(255),
    pnl FLOAT,
    pnl_percent FLOAT,
    pnl_usd FLOAT,
    fees FLOAT DEFAULT 0 NOT NULL,
    fees_currency VARCHAR(16) DEFAULT 'USD' NOT NULL,
    r_multiple FLOAT,
    is_winner BOOLEAN,
    status VARCHAR(32) DEFAULT 'active' NOT NULL,
    raw_signal_data JSONB,
    execution_log JSONB,
    notes TEXT,
    tags JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_trades_user_id ON trades(user_id);
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_account_type ON trades(account_type);
CREATE INDEX idx_trades_strategy ON trades(strategy);

-- Exchange Connections table
CREATE TABLE IF NOT EXISTS exchange_connections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exchange_name VARCHAR(64) NOT NULL,
    exchange_label VARCHAR(128),
    api_key_encrypted TEXT NOT NULL,
    secret_key_encrypted TEXT NOT NULL,
    passphrase_encrypted TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    is_testnet BOOLEAN DEFAULT FALSE NOT NULL,
    permissions VARCHAR(128) DEFAULT 'trade' NOT NULL,
    last_checked_at TIMESTAMPTZ,
    connection_status VARCHAR(32) DEFAULT 'unknown' NOT NULL,
    connection_error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_exchange_user_id ON exchange_connections(user_id);

-- Risk Settings table
CREATE TABLE IF NOT EXISTS risk_settings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_type VARCHAR(16) DEFAULT 'demo' NOT NULL,
    name VARCHAR(128) DEFAULT 'Default Risk Profile' NOT NULL,
    is_default BOOLEAN DEFAULT FALSE NOT NULL,
    max_risk_per_trade_pct FLOAT DEFAULT 1.0 NOT NULL,
    max_daily_risk_pct FLOAT DEFAULT 3.0 NOT NULL,
    max_weekly_risk_pct FLOAT DEFAULT 6.0 NOT NULL,
    max_monthly_risk_pct FLOAT DEFAULT 12.0 NOT NULL,
    max_open_positions INTEGER DEFAULT 3 NOT NULL,
    max_concurrent_same_symbol INTEGER DEFAULT 1 NOT NULL,
    max_leverage INTEGER DEFAULT 10 NOT NULL,
    position_sizing_method VARCHAR(32) DEFAULT 'risk_based' NOT NULL,
    fixed_position_size FLOAT DEFAULT 100.0 NOT NULL,
    kelly_fraction FLOAT DEFAULT 0.25 NOT NULL,
    fractional_multiplier FLOAT DEFAULT 1.0 NOT NULL,
    risk_reward_min_ratio FLOAT DEFAULT 1.5 NOT NULL,
    min_confidence_score FLOAT DEFAULT 0.0 NOT NULL,
    stop_loss_type VARCHAR(32) DEFAULT 'fixed' NOT NULL,
    atr_period INTEGER DEFAULT 14 NOT NULL,
    atr_multiplier FLOAT DEFAULT 2.0 NOT NULL,
    trailing_stop_activation_pct FLOAT DEFAULT 1.0 NOT NULL,
    trailing_stop_distance_pct FLOAT DEFAULT 0.5 NOT NULL,
    take_profit_strategy VARCHAR(32) DEFAULT 'partial' NOT NULL,
    tp1_pct FLOAT DEFAULT 50.0 NOT NULL,
    tp2_pct FLOAT DEFAULT 30.0 NOT NULL,
    tp3_pct FLOAT DEFAULT 20.0 NOT NULL,
    allowed_symbols JSONB,
    blocked_symbols JSONB,
    allowed_asset_classes JSONB,
    trading_hours_start_utc VARCHAR(5),
    trading_hours_end_utc VARCHAR(5),
    blacklisted_days JSONB,
    daily_loss_lock_enabled BOOLEAN DEFAULT TRUE NOT NULL,
    daily_loss_reset_hour_utc INTEGER DEFAULT 0 NOT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_risk_user_id ON risk_settings(user_id);

-- Admin Settings table
CREATE TABLE IF NOT EXISTS admin_settings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    setting_key VARCHAR(128) UNIQUE NOT NULL,
    setting_value TEXT NOT NULL,
    description VARCHAR(512),
    category VARCHAR(64) DEFAULT 'general' NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_admin_settings_key ON admin_settings(setting_key);

-- Admin Notifications table
CREATE TABLE IF NOT EXISTS admin_notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    notification_type VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    severity VARCHAR(16) DEFAULT 'info' NOT NULL,
    related_user_id UUID,
    related_subscription_id UUID,
    is_read BOOLEAN DEFAULT FALSE NOT NULL,
    read_at VARCHAR(64),
    metadata_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_admin_notif_type ON admin_notifications(notification_type);
CREATE INDEX idx_admin_notif_read ON admin_notifications(is_read);

-- ── Seed Admin User & Default Settings ─────────────────
-- Run after tables are created:

-- Insert default admin (telegram_id = 0 placeholder — update with real ID)
-- INSERT INTO users (telegram_id, telegram_username, email, full_name, role, status, approved_by_admin)
-- VALUES (0, 'admin', 'amanossama@gmail.com', 'SmAttaker Admin', 'admin', 'active', TRUE);

-- Default admin settings
INSERT INTO admin_settings (setting_key, setting_value, description, category) VALUES
    ('subscription_price', '99', 'Monthly subscription price in USD', 'subscription'),
    ('trial_days', '3', 'Free trial duration in days', 'subscription'),
    ('max_users', '1000', 'Maximum number of users allowed', 'general'),
    ('maintenance_mode', 'false', 'Enable maintenance mode', 'general'),
    ('welcome_message_en', '🦅 Welcome to SmAttaker! The ultimate AI-powered trading system.', 'Welcome message (English)', 'bot'),
    ('welcome_message_ar', '🦅 مرحباً بك في SmAttaker! نظام التداول الذكي الأقوى.', 'Welcome message (Arabic)', 'bot'),
    ('min_confidence_score', '70', 'Minimum confidence score for signal broadcast', 'trading'),
    ('max_daily_signals', '50', 'Maximum signals per day', 'trading')
ON CONFLICT (setting_key) DO NOTHING;
