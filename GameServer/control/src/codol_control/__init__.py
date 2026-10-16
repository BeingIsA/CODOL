"""CODOL 控制服务（M1）。

独立于游戏客户端启动的管理服务：提供管理 UI、管理员认证、健康/能力查询、
M0 原生运行时探测与诊断数据。分层与边界见 docs/adr/0002 与 0003：

- domain/          纯领域规则与状态定义，不导入框架/数据库/Win32
- application/     用例编排，依赖接口（时钟、存储、探测），不接触 HTTP
- transport/       FastAPI 路由与鉴权，唯一允许导入 Web 框架的层
- infrastructure/  SQLite、令牌、设置、日志等实现
- runtime/         原生运行时契约加载与探测（契约见 GameServer/contracts/）
"""

__version__ = "0.1.0"
