# 大厅启动链路

## 2026-10-16：M3 当前结构补充

M3 在此基础上新增**第三条通道**：原生引擎自己的 UDP 战斗连接，Python 宿主全程不触碰。
新增/改动的客户端入口：

- `native/match_transport2697.h`：入场连接初始化与投影消费的地址/字节布局（移植自旧项目
  `client_native_loopback.cpp`，仅作参考），地图与模式**参数化**、非回环入场地址强制收敛到 127.0.0.1。
- `v2697/room_mailbox.py`：把房间快照里的入场信息写成本地 `m3-match.bin` 投影，按 revision 去重，防重试风暴。
- `v2697/match_events.py`：宿主侧的离场事件分类白名单，把服务端投递的良性
  `EXE_MATCHENDED` 与真正的引擎错误区分开（不得把前者当崩溃处理）。

服务端对局生命周期（启动独立原生战斗进程、`sh1x_endmatch` 优雅停局、房间离开语义）见
`GameServer/docs/M3验收与接入报告.md` 与 `docs/adr/0005`、`docs/adr/0006`。

## 2026-10-15：M2 结构补充

以下大厅链路说明保留原有职责。M2 新增独立玩家通道和版本桥接，已实际连接控制服务：
`app/context.py` 在线装配 → `online/channel.py`（HTTP/SSE、身份与会话）→
`v2697/room_mailbox.py`（意图/投影协议转换）→ `room_staging.py`（副本补丁及安全队列）→
`Client/native/room_bridge2697.cpp`（原生房间）与 preview/flash 适配文件。

权威房间由 `GameServer/control` 的应用/领域层及 SQLite 管理，客户端仅发送意图与显示状态。
网络后台线程不直接调用 UI；原生线程通过安全队列消费带 nonce/序号的有界邮箱。
用户档案继续本地保存，服务端生成的开发 native_id 与档案/装备对应；
`install_identity` 必须分别遵循 AS 与标准 getter 两处不同的寄存器约定。

原生地址和布局未进入通用玩家通道或服务端领域层；来源见 `m2-provenance.json`。
M2 房间交互、Bot名单/断服重连集成与自动依赖约束已通过验收；
详见 `../../GameServer/docs/M2验收与接入报告.md` 和 ADR 0004。

## 本阶段实现的服务方式

目前的宿主进程向本机客户端提供档案、仓库和资源，不监听登录协议端口。游戏执行的是恢复后的 2.6.9.7 副本；安装目录中的原始 EXE 保持不变。官方认证对象不存在时，原始前端会一直等待，旧项目的研究已找到本地就绪路径。本项目沿用这个事实，不声称已有完整服务端。

未来的房间、匹配、聊天和对战协议应作为独立模块实现，不再塞进启动入口。

## 分层结构

源码按依赖方向分层（详见 `src/codol/__init__.py` 的包说明）：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 入口层 | `cli.py`、`__main__.py` | 命令行解析与分发 |
| 应用层 | `app/` | 会话锁（session_lock）、装配（context）、命令执行体（lifecycle）、历史副本回收（retention） |
| 领域层 | `domain/` | 档案（profile_store）、结构捕获（playerdata_schema）、进度（progression）、设置（settings_store）、就绪判定（readiness） |
| 资源供给层 | `resources/` | IFS 索引与补件（provider）、解码（codec） |
| 基础设施层 | `infra/` | Win32 绑定（win32_api）、文件（fsutil）、受限目录（stage_access） |
| 版本适配层 | `v2697/` | 2.6.9.7 常量（client）、补丁（patches）、副本构建（staging）、仓库扩容（inventory_layout）、资源门（resource_gate）、弹窗分类（dialogs）、调试会话（session） |
| 共享内核 | `config.py`、`paths.py` | 配置校验与目录常量 |

2026-10-15 重构前的旧模块名对应关系：`launcher.py` 拆为 `cli.py` 与 `app/`；`session.py`、`patches.py`、`staging.py`、`inventory_layout.py`、`resource_gate.py`、`dialogs.py` 移入 `v2697/`；`profiles.py`、`schema.py`、`progression.py`、`settings.py`、`status.py` 移入 `domain/` 并更名；`win32.py`、`files.py`、`stage_access.py` 移入 `infra/` 并更名；`resources.py` 与 `codec.py` 合并为 `resources/` 包。`docs/provenance.json` 的 module 字段仍记录重构前的位置，以本表为准。

## 生命周期

1. `Config.validate()` 校验客户端和恢复镜像的 SHA-256，检查运行组件。
2. `exclusive_session()` 用 Windows 文件锁禁止并发启动，防止档案互相覆盖。
3. 用户名映射到独立存档目录。第一次启动使用 `seed/profile.json`。
4. `resources.provider.Provider` 索引已安装的 IFS 文件。只为当前运行目录提供受支持的资源类型。
5. `v2697.staging.build()` 从恢复镜像生成副本，应用大厅启动补丁，扩大仓库池，装入本地身份。
6. `v2697.session.run()` 创建暂停的低完整性客户端。运行保护组件确认进程隔离后才恢复执行。
7. 原生初始化过程中，宿主填入经过结构与哈希校验的档案和仓库数据，完成客户端原有回调。
8. `LobbyObserver` 连续观察三秒的界面、角色、仓库和装备状态，写入 `status.json`。
9. 停止请求、超时、异常或客户端退出触发清理。档案稳定时使用临时文件与原子替换保存；保护组件关闭后结束本次会话。

## 为什么仍有底层移植代码

客户端地址、x86 调用约定、档案结构和 Windows 调试事件布局是旧项目的有效研究成果。`v2697/patches.py`、`v2697/session.py` 等底层模块由这些成果整理而来，而非独立的净室实现。移植记录与原文件哈希在 `provenance.json`；每个移植模块也有来源说明。

已去掉旧启动器的函数源代码字符串替换、动态 `exec`、房间 runner 链式导入和诊断开关矩阵。版本补丁的原字节检查保留，因此禁止用 `python -O` 运行。`tools/import_research.py` 是本次一次性迁移的记录，拒绝覆盖现有维护源码，不是日常构建入口。

对“完全重写”的边界需要明确：上层应用已经重新实现，但底层调试桥接与部分数据算法仍为移植代码。后续可在现有端到端验收基础上逐块替换，不能将当前版本称为全部代码从零重写。

## 已知行为

原生退出页面可打开但不能可靠结束进程，内部根因尚未单独证明。使用 `Stop.cmd` 会向当前项目的活动会话写入停止请求，由该会话保存档案并关闭自己的进程；不会扫描或结束其他游戏进程。

大厅就绪指标说明数据与界面状态已初始化，不等同于全部按钮或游戏模式可用。实机画面验收单独记录，异常退出码和保存失败不会被大厅就绪状态掩盖。

`assets` 是独立复制的本地研究资产，不链接到旧项目。会话中静态资源可能硬链接到这个新项目自己的资产副本以缩短准备时间；代码不修改这些静态文件。设置和档案保存在独立可写目录。
