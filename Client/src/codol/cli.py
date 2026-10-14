"""入口层：命令行解析与命令分发。

支持的命令（``python -m codol <命令>``）：
- ``check``   只做版本/组件校验，不打开游戏；
- ``stage``   只生成客户端副本，不打开游戏；
- ``launch``  正常启动，直到会话结束（默认命令）；
- ``smoke``   限时启动，大厅就绪即自动结束（需 --seconds）；
- ``stop``    请求结束当前活动会话；
- ``clean``   回收全部历史会话的客户端副本（证据 JSON 保留）。

本模块只做解析与分发，业务在各命令执行体（app.lifecycle）里。
"""
import argparse
import json
import sys
from pathlib import Path
from .config import Config, username
from .app import lifecycle


def build_parser():
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(description='CODOL 2.6.9.7 本地大厅')
    parser.add_argument('command', nargs='?', choices=['check', 'stage', 'launch', 'smoke', 'stop', 'clean'], default='launch')
    parser.add_argument('--game', type=Path)
    parser.add_argument('--username')
    parser.add_argument('--seconds', type=int, default=None)
    return parser


def main(argv=None):
    """解析参数并分发到对应执行体，返回进程退出码。"""
    parser = build_parser()
    options = parser.parse_args(argv)
    # stop 与 clean 不依赖游戏目录与配置，放在一切校验之前。
    if options.command == 'stop':
        return lifecycle.request_stop()
    if options.command == 'clean':
        return lifecycle.clean_runtimes()
    if options.seconds is not None and not 0 <= options.seconds <= 3600:
        parser.error('--seconds must be 0..3600')
    if options.command == 'smoke' and options.seconds == 0:
        parser.error('smoke requires a bounded timeout')
    # 底层校验依赖 assert，禁止 -O 运行。
    if sys.flags.optimize:
        raise RuntimeError('不能用 Python -O 运行：客户端校验必须保留。')
    config = Config.load()
    config = Config(options.game.resolve() if options.game else config.game,
                    username(options.username) if options.username else config.name)
    checked = config.validate()
    print('客户端与运行组件校验通过。', flush=True)
    if options.command == 'check':
        print(json.dumps(checked, ensure_ascii=False, indent=2))
        return 0
    # launch 的默认时限为 0（不限时），smoke 必须显式给时限。
    seconds = options.seconds if options.seconds is not None else (180 if options.command == 'smoke' else 0)
    if options.command == 'stage':
        return lifecycle.run_stage(config, options.command, seconds)
    return lifecycle.run_session(config, options.command, seconds)


def entry():
    """进程入口：捕获异常，转成用户可读的失败信息与退出码 1。"""
    try:
        return main()
    except Exception as exc:
        import traceback
        print('启动失败：' + str(exc), flush=True)
        traceback.print_exc()
        return 1
