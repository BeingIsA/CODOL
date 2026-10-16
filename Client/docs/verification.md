# 验证记录 · 2026-10-14

## 2026-10-16：M3 最终验收完成（当前状态）

- 客户端从原生房间**真实入场**到独立原生战斗进程：服务端 `primed_to_active=1`、
  `active_uids` 命中成员 native_id；停局后客户端明确断开并回房，房间恢复 `OPEN`，可再次开局。
- 全自动主路径 `verify_m3_match.py --admin-start`：汇总 `GameServer/manifests/m3/match-integration.json`
  `passed=true`；两局均 `FINISHED`、`publication_verified=true`、无致命日志；客户端侧入场/世界就绪/断开/回房各 2 次、连接重置 0。
- 原生按钮路径（人工）：大厅「创建房间」→「开始游戏」→ 地图内出生/移动/视角/开火/换弹正常；
  原生「返回大厅」后服务端受理离开，房间 `CLOSED`（rev5）、席位清空、对局 `FINISHED`（证据 `leave-path-*.json`）。
- 回归：控制服务 72 项、客户端 23 项、GameServer 7 项（1 项协议基线如实 skip）；桥接 DLL 与 EXE 哈希见 `Client/docs/m3-provenance.json`。
- 仍未实现：Bot 实体与 AI、伤害/计分/模式规则、多人联机；两项未定位的引擎故障见 M3 报告 §5。

## 2026-10-15：M2 最终验收完成（下方为历史记录）

- 客户端17项、控制服务41项回归通过，最终桥接DLL的PE/身份/安全队列安装检查通过。
- 最终会话 `d44fe04c`：原生按钮新建房间，Bot3→1→0、阵营/难度投影、断服离线、同房间重启恢复、原生离房及guard清理全部通过。
- 两端实际管理UI提交和游戏画面另见 `visual-3f2700f2/`；最终画面 `visual-d44fe04c/`、退出/通信日志 `projection-d44fe04c/` 位于 `GameServer/manifests/m2/`。
- 汇总 `projection-integration.json` passed=true、creation=native-button；来源和哈希 `Client/docs/m2-provenance.json`，文件清单与观察记录 `GameServer/manifests/m2/final-acceptance.json`。
- 本次修正服务协议1/2与原生大厅0/1的阵营转换、零基房间等级。之前UID特殊调用点修复继续保留，两处getter不能套同一寄存器顺序。
- AST依赖约束已落地，M1恒空房间类型已清理。**当时**仍未接入真实战斗/Bot实体/多人——其中真实战斗入场已于 2026-10-16（M3）完成，Bot 实体与多人仍未实现；结构任务与关口见 M2 报告 §5/§8 与 M3 报告 §6.1。

下方历史会话的可访问性以 AGENTS.md 及当前实际目录为准，不将旧记录自动视为当前可复查证据。

在当前 Windows 机器和 `Original Client` 的 2.6.9.7 安装上完成。

| 检查 | 结果 |
| --- | --- |
| 原始客户端、恢复镜像、运行保护组件哈希 | 通过 |
| `stage`：独立客户端副本与身份补丁生成 | 通过 |
| `smoke`：启动并连续观测大厅就绪 | 通过，`e53d0c92` |
| 实际大厅画面 | 已看到 Player、70 级、尤里模型、武器预览与大厅导航 |
| 仓库导航 | 点击后打开原生仓库，看到 BPR2000、AA-12、ACR、AK 系列等武器条目 |
| 原生退出按钮 | 不通过：退出页面可打开，但点击退出未结束进程；使用独立停止入口 |
| `verify_stop.py`：启动 → 大厅就绪 → CLI 停止 → 清理 | 通过，`bbefe772` |
| 回归测试 | 9 项通过 |
| Python 源码编译检查 | 通过 |

实机大厅/仓库观察来自会话 `81c26ced`，该会话最终按 180 秒上限结束。它不是正常退出成功的证据。

最终停止集成测试会话 `bbefe772` 满足：

- `lobby_ready = true`
- `inventory_count = 6584`
- `character = 545`
- `loadout_state = 4`
- `summary.errors = []`
- 已创建 `stop.request`，结束原因不是超时
- `child_terminated = true`
- `guard_cleanup.engine_close_status = 0`
- `data/active.json` 已清除

原始日志保存在 `../../.lobby/<会话编号>/`。

测试覆盖用户名编码与长度、存档往返写入、损坏存档备份恢复、无备份时保留损坏文件、就绪状态稳定性、缺失仓库拒绝判定成功、读进程失败重置稳定窗口、异常退出/清理失败识别、停止请求。

未验收：实际对战、房间、匹配、联网商城/社交、全部角色与装备组合、长时间稳定性。大厅活动图为空、部分角色标签名称异常仍属于已知限制。

## 变更记录 · 2026-10-15 角色变体去重

现象：大厅仓库中出现同名重复角色卡片（如 4 个威、3 个约翰、2 个牛仔）。
定位：`assets/local-inventory-full/inventory.json` 把目录里全部 35 条 `cha` 条目写入仓库池；
其中部分兵种的变体（`baseA~E`/`veteran`/`prestige`/`elite`）与基础条目显示同名（如 ACU 四条"威"），
客户端按本地化显示名逐条显示，不去重。写入代码与目录核对本身无误。
修复：同显示名的变体每名只留一条（35 → 15 条，总数 6584 → 6564）；用户反馈后确认变体不总是同名
（SRT 兵种 1901"战火"与 1902"黑桃Q"是独立角色），恢复 1902 后为 16 条、总数 6565。
`counts`、`tests/test_lobby.py` 假状态与各文档同步。
旧清单备份：`assets/local-inventory-full/inventory.json.previous-cha-dedup`。
静态校验（ID 唯一、目录逐条核对、capacity）通过；本节历史记录中的 `inventory_count = 6584` 为去重前的真实值。
待验收：实机进入仓库确认角色卡片不再重复（观察点只看物品总数，覆盖不到显示名去重视觉效果）。

### 附：CHARACT 占位名与角色显示名去重（2026-10-15，已收口）

- 多轮静态排查确认：全部已解码 fastfile 的明文与 zlib 压缩块（GBK/UTF-8，含 74MB `common_mp.ff`）、
  恢复镜像、`codoMP_client_shipRetailBase.dll`、`decoded-resources/main`、原始客户端散装文件中
  均无任何角色中文名；但"黑桃Q""鹰眼""战火"等名字实机可正常显示——名字数据在 IFS 运行时读取
  或服务器侧，本地静态不可见，显示名只能靠用户实机核对获得。
- 采集轮（31 条角色在库，总数 6580，会话 `fede7f51`）用户报告显示名分布：
  约翰×3、马库斯×3、尤里×3（INF 545/546/547）、黑百合×2（SHE 642/649）、弗拉基米尔×3、
  幽灵（571）与战火（570）模型重复、CHARACT（1901，名字键本地无法解析）多余。
- 最终去重：同名变体留一条，异名保留（鹰眼 569+战火 570、黑桃Q 1902）；按用户要求删除
  幽灵 571 与 CHARACT 1901；20 条角色、总数 6569。名字对照表见
  `assets/local-inventory-full/README.md`。
- 原版机制（分析结论）：服务器按账号每兵种只下发一个激活变体，名字表由服务器随版本推送 patch
  fastfile 保持完整——重复卡片与占位名在原版结构性不可能。我们的两个现象分别源于
  "全量目录当拥有列表"与"patch 数据缺失"。
- 待办：CHARACT 若需根治，须逆向 fastfile 字符串表格式后本地伪造 patch 名字数据（独立任务）。
