-- M2：独立开发身份、短期会话、房间快照与幂等结果。
-- 原 M1 rooms 表继续保留查询索引；快照是成员/Bot 配置的完整版本投影。
CREATE TABLE players (
    development_key TEXT PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    native_id INTEGER NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);
CREATE TABLE player_sessions (
    token_hash TEXT PRIMARY KEY,
    player_id TEXT NOT NULL REFERENCES players(id),
    expires_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE room_snapshots (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id),
    payload TEXT NOT NULL
);
CREATE TABLE command_receipts (
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(actor, request_id)
);
