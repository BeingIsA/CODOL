-- 0003：M3 对局生命周期。
-- matches 表在 0001 已建（id/room_id/state/created_at/updated_at），本迁移
-- 补充运行期事实：冻结配置、运行目录与进程标识、入场信息、结果与失败原因。
-- 设计依据：docs/adr/0005-M3独立战斗连接与入场链路.md；
-- 房间状态沿用 rooms.state 枚举（STARTING/IN_MATCH 已在 0001 预留），
-- 面向客户端的对局视图放在房间快照 JSON 的 match 字段，不在此建表。

ALTER TABLE matches ADD COLUMN config     TEXT NOT NULL DEFAULT '{}'; -- 冻结配置 JSON（地图/模式/成员/席位）
ALTER TABLE matches ADD COLUMN run_dir    TEXT NOT NULL DEFAULT '';  -- .server-runs/<id> 运行目录
ALTER TABLE matches ADD COLUMN pid        INTEGER;                   -- 战斗进程 PID（服务重启后核对归属用）
ALTER TABLE matches ADD COLUMN port       INTEGER;                   -- 本局 UDP 端口（M3 固定 28969）
ALTER TABLE matches ADD COLUMN entry      TEXT NOT NULL DEFAULT '{}'; -- 入场信息 JSON（host/port/map/gametype）
ALTER TABLE matches ADD COLUMN result     TEXT NOT NULL DEFAULT '{}'; -- 结果摘要 JSON（humans 采样、退出码等）
ALTER TABLE matches ADD COLUMN error      TEXT NOT NULL DEFAULT '';  -- 失败/中断原因（人读）
ALTER TABLE matches ADD COLUMN finished_at TEXT;                     -- 进入终态的时间

CREATE INDEX IF NOT EXISTS idx_matches_room ON matches(room_id);
CREATE INDEX IF NOT EXISTS idx_matches_state ON matches(state);
