"""领域层：纯规则与状态定义。

禁止导入 FastAPI、sqlite3、Win32、路径常量或任何框架符号（ADR 0002）。
本层抛出的领域错误由 transport 层统一映射为 HTTP 响应。
"""
