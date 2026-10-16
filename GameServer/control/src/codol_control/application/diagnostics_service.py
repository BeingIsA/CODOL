"""诊断查询用例：委托只读证据端口，文件系统细节位于基础设施。"""
from .ports import Diagnostics

class DiagnosticsService:
    """不拥有现场写入和回收权限的管理查询入口。"""
    def __init__(self, reader: Diagnostics):
        """组合根注入已脱敏的只读证据源。"""
        self._reader = reader

    def recent_log_entries(self, limit=100):
        """获取有界日志尾部，具体来源由证据适配器决定。"""
        return self._reader.recent_log_entries(limit)

    def list_server_runs(self):
        """列出既有运行证据，不由查询启动或清理原生进程。"""
        return self._reader.list_server_runs()

    def bundle(self):
        """导出同一证据源提供的脱敏诊断集合。"""
        return self._reader.bundle()
