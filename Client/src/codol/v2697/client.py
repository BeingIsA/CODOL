"""版本适配层：2.6.9.7 客户端的共享常量与定位信息。

集中定义被补丁（patches）、副本构建（staging）和调试会话（session）
共同引用的版本常量：原始客户端目录、恢复镜像路径与哈希、禁载组件清单。

注意：本模块在导入期读取 ``CODOL2697_GAME_DIR`` 环境变量，
必须由 app.context.prepare 在导入本模块之前设置。
"""
import os
from pathlib import Path
from ..paths import ASSETS as ROOT

# 原始客户端安装目录（只读，绝不修改或启动其中的 EXE）。
GAME = Path(os.environ['CODOL2697_GAME_DIR'])
# aPLib 解压恢复镜像：所有静态补丁的基底；哈希与 config.RECOVERED_HASH 一致。
SOURCE = ROOT / 'aplib-recovered/codol2697-static-candidate.bin'
SOURCE_SHA = '24b4ef2502d3661df178f557192534098c714353cf8c2a00a2ffb26af2bb97e4'
# 隔离副本中禁止加载的旧保护/平台组件：一旦发现立即停止会话。
FORBIDDEN = {'tersafe.dll', 'tenslx.dll', 'tp3helper.exe', 'codomp_client_shipretailbase.dll'}
