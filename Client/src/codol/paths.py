"""共享内核：本项目的全部目录常量。

所有可写位置都由本项目拥有，集中在这一处定义，避免路径散落各模块。
本模块不依赖包内任何其他模块，可以安全地被单独导入（单元测试也直接使用）。
"""
from pathlib import Path

# 项目根目录：src/codol/paths.py 向上两级即 "Client"。
PROJECT = Path(__file__).resolve().parents[2]
# 底层资产（恢复镜像、解码资源、种子数据等），内容说明见 assets/README.md。
ASSETS = PROJECT / 'assets'
# 用户数据：按用户名摘要分目录存档、会话锁与活动会话标记。
DATA = PROJECT / 'data'
# 每次运行的客户端副本目录。放在工作区上一级的 .lobby 以保持较短路径：
# 旧引擎内部存在小的定长路径缓冲区，路径过长会出问题。
RUNS = PROJECT.parent / '.lobby'
# 原始客户端安装目录，默认为工作区旁的 "Original Client"，只读不修改。
DEFAULT_GAME = PROJECT.parent / 'Original Client'
