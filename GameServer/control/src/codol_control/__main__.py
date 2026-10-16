"""python -m codol_control 入口：透传参数给 main.run。

可用参数：--no-browser（启动后不自动打开管理页）。
"""
from .main import run

if __name__ == "__main__":
    raise SystemExit(run())
