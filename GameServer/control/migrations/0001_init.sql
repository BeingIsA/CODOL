-- 0001：M1 初始结构。
-- 设计依据：Client/docs/服务端与Bot对战实施方案.md §5.3/§5.4/§6。
-- rooms / room_participants / matches / events_out 在 M1 建表但不产生业务数据，
-- 为 M2（客户端建房）与 M3（开局）预留唯一权威存储；管理凭据在 settings 表。

CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,             -- 房间 ID，服务端生成
    name        TEXT NOT NULL,                -- 显示名
    state       TEXT NOT NULL DEFAULT 'OPEN', -- OPEN/STARTING/IN_MATCH/CLOSED
    revision    INTEGER NOT NULL DEFAULT 1,   -- 单调递增版本，乐观并发用
    map         TEXT NOT NULL DEFAULT '',     -- 地图（快照字段，未开局为空）
    gametype    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS room_participants (
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    slot        INTEGER NOT NULL,             -- 席位号，同房间唯一
    kind        TEXT NOT NULL,                -- human/bot（M2 起写入）
    display_name TEXT NOT NULL DEFAULT '',
    ready       INTEGER NOT NULL DEFAULT 0,
    joined_at   TEXT NOT NULL,
    PRIMARY KEY (room_id, slot)
);

CREATE TABLE IF NOT EXISTS matches (
    id          TEXT PRIMARY KEY,             -- 对局 ID，一次性使用
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    state       TEXT NOT NULL,                -- ALLOCATING..FINISHED/FAILED/CANCELLED
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 事件外发表（方案 §5.4 第 3 条）：写库与待发布事件同事务记录，
-- M2 起由推送通道消费后标记 delivered_at；避免写库成功却漏通知。
CREATE TABLE IF NOT EXISTS events_out (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    delivered_at TEXT
);

-- 敏感操作审计（登录、改密、停服、探测触发等）。
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);

-- 键值设置：管理员凭据散列、口令版本、令牌签名密钥等安装级数据。
-- 值为 JSON 字符串；读写经 infrastructure/settings_store.py。
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
