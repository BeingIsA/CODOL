"""M2 分层依赖约束：业务不依赖框架、存储实现或原生适配。

使用 AST 解析全部导入（包括函数内和相对导入），避免仅靠目录命名声称分层。
这里只允许标准库和明确的内层模块；新增第三方依赖需先审视边界。
"""
import ast
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]


class ArchitectureTests(unittest.TestCase):
    """将 ADR 0002/0004 的业务与版本隔离约束落实到源码检查。"""

    def test_inner_layer_imports(self):
        """业务层只导入标准库及内层；玩家协议不能反向导入 Win32/版本模块。"""
        cases = [
            (ROOT / 'GameServer/control/src/codol_control/domain', 'codol_control.domain',
             ('codol_control.domain',)),
            (ROOT / 'GameServer/control/src/codol_control/application', 'codol_control.application',
             ('codol_control.domain', 'codol_control.application')),
            (ROOT / 'Client/src/codol/online', 'codol.online', ('codol.online',)),
        ]
        for folder, package, allowed in cases:
            for path in folder.rglob('*.py'):
                suffix = path.parent.relative_to(folder).parts
                context = '.'.join((package, *suffix))
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [importlib.util.resolve_name('.' * node.level + (node.module or ''), context)
                                   if node.level else node.module or '']
                        # 根包只允许读取版本标识，防止借此导入外层模块。
                        if modules == ['codol_control'] and all(a.name == '__version__' for a in node.names):
                            continue
                    for module in modules:
                        with self.subTest(file=str(path.relative_to(ROOT)), line=node.lineno, module=module):
                            self.assertNotIn(module.split('.')[0], {'ctypes', 'winreg', 'sqlite3', 'socket', 'subprocess'},
                                             'OS/存储/网络实现必须留在外层适配器。')
                            self.assertTrue(module.split('.')[0] in sys.stdlib_module_names or
                                            any(module == item or module.startswith(item + '.') for item in allowed),
                                            '跨层依赖，请通过 application/ports 注入实现。')
