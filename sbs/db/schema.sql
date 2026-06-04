-- Stock Breakout Scanner Platform — database schema.
-- Portable SQL (SQLite dev / PostgreSQL prod). Dates are ISO-8601 TEXT.
-- JSON payloads are stored as TEXT for portability.

-- ---------------------------------------------------------------------------
-- Reference data (survivorship-bias-free): keep delisted/acquired/bankrupt rows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS securities (
    symbol         TEXT PRIMARY KEY,
    name           TEXT,
    exchange       TEXT,
    security_type  TEXT DEFAULT 'COMMON',
    sector         TEXT DEFAULT 'Unknown',
    market_cap     REAL DEFAULT 0,
    listing_date   TEXT,
    delisting_date TEXT,            -- NULL => still listed
    status         TEXT DEFAULT 'active',  -- active|delisted|acquired|bankrupt|merged
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS exchange_history (
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT,
    PRIMARY KEY (symbol, start_date)
);

-- Point-in-time fundamentals (filed quarterly; keyed by report/filing date so
-- backtests never use restated/future figures).
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol               TEXT NOT NULL,
    report_date          TEXT NOT NULL,    -- date the figure became public
    eps_ttm              REAL,
    revenue_ttm          REAL,
    eps_growth_yoy       REAL,
    revenue_growth_yoy   REAL,
    shares_outstanding   REAL,
    PRIMARY KEY (symbol, report_date)
);

-- ---------------------------------------------------------------------------
-- Data versioning (reproducibility)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_versions (
    version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT NOT NULL,
    provider_version  TEXT,
    download_date     TEXT NOT NULL,
    interval          TEXT DEFAULT '1d',
    symbol_count      INTEGER DEFAULT 0,
    universe_version  TEXT,
    start             TEXT,
    end               TEXT,
    notes             TEXT
);

-- ---------------------------------------------------------------------------
-- Universe snapshots (one row per as-of build) + members
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS universe_snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_version TEXT NOT NULL,
    as_of_date       TEXT NOT NULL,
    criteria_json    TEXT,
    member_count     INTEGER DEFAULT 0,
    created_at       TEXT,
    UNIQUE (universe_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS universe_members (
    snapshot_id      INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    market_cap       REAL,
    avg_volume       REAL,
    avg_dollar_vol   REAL,
    sector           TEXT,
    PRIMARY KEY (snapshot_id, symbol),
    FOREIGN KEY (snapshot_id) REFERENCES universe_snapshots(snapshot_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Signals (auditable) + version stamps + regime
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    signal_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy          TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    signal_date       TEXT NOT NULL,
    direction         TEXT DEFAULT 'long',
    entry_price       REAL,
    stop_price        REAL,
    target_price      REAL,
    score             REAL,
    trigger_reason    TEXT,
    indicator_values  TEXT,   -- JSON
    filter_values     TEXT,   -- JSON
    score_factors     TEXT,   -- JSON: per-factor ranking breakdown (journal "drivers")
    regime            TEXT,
    config_version    TEXT,
    universe_version  TEXT,
    data_version_id   INTEGER,
    created_at        TEXT,
    UNIQUE (strategy, symbol, signal_date),
    FOREIGN KEY (data_version_id) REFERENCES data_versions(version_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_strategy_date ON signals(strategy, signal_date);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

-- Forward performance per horizon (1/5/10/20/60/90d)
CREATE TABLE IF NOT EXISTS signal_performance (
    signal_id      INTEGER NOT NULL,
    horizon_days   INTEGER NOT NULL,
    eval_date      TEXT,
    price          REAL,
    return_pct     REAL,
    r_multiple     REAL,
    PRIMARY KEY (signal_id, horizon_days),
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id) ON DELETE CASCADE
);

-- Trade lifecycle transitions
CREATE TABLE IF NOT EXISTS trade_lifecycle (
    signal_id   INTEGER NOT NULL,
    state       TEXT NOT NULL,   -- open|target_hit|stop_hit|expired|cancelled
    as_of_date  TEXT,
    price       REAL,
    note        TEXT,
    PRIMARY KEY (signal_id),
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Backtests + trades
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtests (
    backtest_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy          TEXT NOT NULL,
    strategy_version  TEXT,
    start_date        TEXT,
    end_date          TEXT,
    walkforward       INTEGER DEFAULT 0,
    config_json       TEXT,
    metrics_json      TEXT,
    config_version    TEXT,
    universe_version  TEXT,
    created_at        TEXT
);

-- Downsampled equity curve per backtest (for report charts). A separate table so
-- it is added idempotently to existing DBs via CREATE TABLE IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS backtest_equity (
    backtest_id  INTEGER PRIMARY KEY REFERENCES backtests(backtest_id),
    points_json  TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    trade_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id   INTEGER NOT NULL,
    symbol        TEXT,
    entry_date    TEXT,
    entry_price   REAL,
    exit_date     TEXT,
    exit_price    REAL,
    shares        INTEGER,
    exit_reason   TEXT,           -- target|stop|trail|expire|eod
    return_pct    REAL,
    r_multiple    REAL,
    pnl           REAL,
    bars_held     INTEGER,
    regime        TEXT,
    FOREIGN KEY (backtest_id) REFERENCES backtests(backtest_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_backtest ON backtest_trades(backtest_id);

-- ---------------------------------------------------------------------------
-- Market regime (one row per date)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_regime (
    as_of_date   TEXT PRIMARY KEY,
    benchmark    TEXT,
    trend_state  TEXT,            -- bull|bear|sideways
    vol_state    TEXT,            -- high_vol|low_vol|normal_vol
    detail_json  TEXT
);

-- ---------------------------------------------------------------------------
-- Paper trading
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy     TEXT,
    symbol       TEXT,
    open_date    TEXT,
    entry_price  REAL,
    shares       INTEGER,
    stop_price   REAL,
    target_price REAL,
    status       TEXT DEFAULT 'open',  -- open|closed
    close_date   TEXT,
    close_price  REAL,
    exit_reason  TEXT,
    pnl          REAL,
    r_multiple   REAL
);
