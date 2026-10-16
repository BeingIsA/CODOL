# CODOL 控制服务（M1 + M2 + M3）

独立于游戏客户端启动的管理服务：管理 UI、管理员认证、健康/能力查询、
原生运行时契约预检（只读）与诊断数据。M2 已加入开发身份、玩家通道、真实房间和 Bot 配置。
M3 已加入对局生命周期：开局按 `native-runtime-v1` 契约启动独立原生战斗进程、客户端真实入场、
停局时先投递 `sh1x_endmatch` 让客户端优雅离场再 `quit`，以及玩家「返回大厅」的离开语义。
**真实 Bot 实体对战在 M4**；Bot 名额目前仍是房间配置席位，不是战斗实体。
M3 功能与结构验收已完成（本机单客户端边界），详见 `../docs/M3验收与接入报告.md`。

## 启动与停止

```bat
GameServer\control\Start.cmd      REM 前台运行并显示日志；首次运行自动开管理页
GameServer\control\Stop.cmd       REM 优雅停服（本机停服令牌）
```

也可以手动运行 `GameServer\python\python.exe -X utf8 -m codol_control --no-browser`
（在 `GameServer\control` 目录下）。管理页地址：`http://127.0.0.1:8790/`。

- **首次启动**会生成 `data/初始管理员凭据.txt`（仅此一次），请登录后立即改密；
- 关闭浏览器页面**不会**停止服务；关闭 Start.cmd 窗口或执行 Stop.cmd 才会；
- 端口被占时进程以退出码 2 结束，并在 `data/startup-failure.json` 写诊断。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/codol_control/` | 分层源码：`domain`（纯规则）/ `application`（用例）/ `transport`（FastAPI，唯一框架层）/ `infrastructure`（SQLite、令牌、日志）/ `runtime`（契约与探测） |
| `ui/` | 管理页静态文件（无构建步骤，`Cache-Control: no-cache`） |
| `migrations/` | SQLite 迁移脚本（按文件名序执行，失败即停启动） |
| `tests/` | 94 项自动化测试（无客户端、无外联；假探测器显著标识 `fake=True`） |
| `config/` | 配置模板；实际 `config.json` 首次启动自动生成，之后绝不回写 |
| `data/`、`logs/` | 运行期数据与 JSONL 日志（不入 Git） |

## 运行环境（GameServer/python）

服务端专属受控环境，与 `Client/python` 完全独立：嵌入式 Python 3.12.0 副本 +
pip 安装的锁定依赖（`requirements.lock.txt`：fastapi 0.143.1、uvicorn 0.54.0、
pydantic 2.14.1）。重建方式：

```powershell
# 1) 复制任意嵌入式 Python 3.12 到 GameServer/python
# 2) python312._pth 写入：python312.zip / DLLs / Lib/site-packages / ../control/src / import site
# 3) 下载 get-pip.py 引导 pip 后安装锁定版本：
#    python -m pip install -r control/requirements.lock.txt
```

环境与依赖不入 Git；离线可复现安装包归 M5 发行任务。

## API 摘要（管理接口均需 Bearer 会话令牌）

| 方法/路径 | 鉴权 | 用途 |
| --- | --- | --- |
| GET `/api/health`、GET `/api/capabilities` | 无 | 存活探测；能力表（未实现能力如实为 false） |
| POST `/api/admin/login` / `password` / GET `session` | 部分 | 登录、改密（改密后旧令牌全部失效）、whoami |
| POST `/api/admin/shutdown` | 会话或 `X-Shutdown-Token` | 优雅停服（Stop.cmd 用本机停服令牌） |
| GET `/api/overview` `/api/rooms` | 会话 | 总览聚合；真实房间列表与当前能力（建房由玩家通道发起） |
| POST `/api/rooms/{room_id}/close` | 会话 | 管理员关闭房间：清空全部成员；对局中先优雅停局再关房 |
| GET `/api/runtime`、POST `/api/runtime/probe` | 会话 | 运行时契约预检缓存 / 触发全量探测（1.1GB 哈希，耗时数十秒） |
| GET `/api/diagnostics/logs` `server-runs` `bundle` | 会话 | 已脱敏日志尾部、原生运行历史、诊断包 |

统一错误格式：`{"error":{"code","message"},"request_id"}`；审计写 `audit_log` 表。
契约来源：`GameServer/contracts/native-runtime-v1.json`（与原生验收器共用）。

## M2 玩家接入

先启动本控制服务，再启动 `Client/StartOnline.cmd`。玩家通道与管理会话分离，所有路径前缀为 `/api`：

| 方法/路径 | 鉴权 | 用途 |
| --- | --- | --- |
| POST `/player/login` | `X-Development-Key` | 本机开发身份登录及协议版本检查 |
| POST `/player/heartbeat`、`/player/disconnect` | 玩家 Bearer | 在线租约、主动断开 |
| POST `/player/rooms`、`/player/leave` | 玩家 Bearer | 原生建房、退出 |
| POST `/player/room-config` | 玩家 Bearer | 自己阵营、房主地图修改，含版本检查 |
| GET `/player/snapshot`、`/player/events` | 玩家 Bearer | 全量快照、SSE 状态/事件推送 |
| POST `/rooms/{room_id}/bots` | 管理员 Bearer | Bot 数量、阵营、难度；容量、幂等与版本校验 |

`data/development-access.json` 为开发接入密钥，`Client/data/<档案>/development-identity.json` 为私有客户端身份；
它们不是管理员登录口令，不入 Git。当前只用于本机开发，尚非正式账号服务器。
房间快照、命令回执和事件在同一 SQLite 事务保存，管理页直接显示权威房间数据。
Bot 配置只是房间名额，能力表真实 `bots` 和 `match_control` 仍为 false。

### 管理员关闭房间（管理页「关闭房间」按钮）

与玩家 `/player/leave` 是两种不同的处置：leave 只移除本人席位（ADR 0006），
关闭是管理端对整个房间的处置——房内所有成员（在线或离线）同时失去席位，
玩家下一次同步拿到空快照即回大厅，房间不再占用活动房间容量。
房间正在对局时，服务端**先按正常路径停止对局**（`sh1x_endmatch` → `quit`），
再关闭房间：直接关房会让客户端等不到离场命令、干等 UDP 超时弹
`EXE_ERR_SERVER_TIMEOUT`。同一 `request_id` 重复提交返回同一结果。

### 无人房间自动回收（客户端异常退出兜底）

客户端崩溃/被强杀/闪退不会发来"离开"，席位会一直留在房间里，而当前只允许
一个活动房间——下一个客户端建房会被 `ROOM_FULL` 拒绝。因此服务端每 15 秒检查
一次：`OPEN` 房间的**所有成员**都超过 `ABANDONED_TIMEOUT_SECONDS = 90` 秒没有任何
心跳时，自动关闭该房间（规则 `domain/room_rules.is_abandoned`，执行
`application/room_reaper.py`）。

- 阈值取 90 秒：心跳 3 秒一次、离线判定 15 秒，90 秒远大于"崩溃 → 重启客户端 →
  重连"的时间，**不会吃掉断线重连恢复**；
- 对局中的房间不归回收管：由对局观察线程按 `humans` 归零先结束对局、房间回
  `OPEN`，之后才轮到回收（两套规则不抢同一个房间）；
- 管理页房间卡片对"全员离线"的房间显示回收提示，管理员也可随时手动关闭。

## 测试

```powershell
.\GameServer\python\python.exe -X utf8 -m unittest discover -s GameServer\control\tests -v
.\GameServer\python\python.exe -X utf8 -m compileall -q GameServer\control\src GameServer\control\tests
```

## 边界与已知限制

- 单进程单 worker；房间状态唯一权威在 SQLite + 领域层（多 worker 属 M6 改造）。
- 管理接口只绑定回环；多人/TLS/速率限制属后续里程碑（方案 §8.1）。
- SQLite 每线程共用一把写锁；M1 负载下足够，遇到并发瓶颈时再议。
- 日志已脱敏（令牌/口令不入日志）；`data/` 含凭据散列，勿随包分发。
