"""应用层：会话装配——把配置变成可执行的会话参数。

``prepare`` 完成三件事：
1. 按用户名摘要建立独立账号目录（首次创建时从 seed 复制档案与设置）；
2. 在导入依赖环境变量的模块之前设置 ``CODOL2697_GAME_DIR``，
   再构造资源提供器与就绪观察者；
3. 组装传给版本适配层（staging/session）的参数对象。

导入顺序是刻意安排的：resources.provider 与 v2697.* 在导入期读取
该环境变量，必须先设置再导入。
"""
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from ..paths import PROJECT, DATA, RUNS
from ..domain.readiness import LobbyObserver
from ..domain import settings_store


def prepare(config, stop_when_ready, seconds, online=False):
    """装配一次会话，返回 (参数对象, 就绪观察者)。

    ``stop_when_ready`` 为 True 时（smoke 命令）大厅就绪即自动结束；
    ``seconds`` 为观察时限（0 = 不限时）。
    """
    os.environ['CODOL2697_GAME_DIR'] = str(config.game)
    from ..resources.provider import Provider
    from ..v2697 import staging
    # 账号目录按用户名 UTF-8 摘要前 20 位划分，天然隔离且避免路径非法字符。
    account = DATA / hashlib.sha256(config.name.encode('utf-8')).hexdigest()[:20]
    account.mkdir(exist_ok=True)
    profile = account / 'profile.json'
    if not profile.exists():
        shutil.copyfile(PROJECT / 'seed/profile.json', profile)
    if not (account / 'settings').exists():
        shutil.copytree(PROJECT / 'seed/settings', account / 'settings')
    # 设置存档库重定向到当前账号（模块级 store 的唯一写入点）。
    settings_store.store = account / 'settings'
    (account / 'identity.json').write_text(json.dumps({'username': config.name}, ensure_ascii=False), encoding='utf-8')
    provider = Provider()
    RUNS.mkdir(exist_ok=True)
    observer = LobbyObserver(stop_when_ready)
    args = SimpleNamespace(
        run_directory=RUNS / uuid.uuid4().hex[:8], profile_path=profile,
        username=config.name, seconds=seconds,
        # -qa 为 QA 模式；窗口化参数在恢复用户设置时会被移除。
        argument=['-qa', '+set', 'r_fullscreen', '0'],
        resource_provider=provider, state_observer=observer,
        stage_client=staging.build, stop_engine_error=True,
        # 临时只读取证开关：定位双持创建失败，默认关闭以免增加游戏线程断点开销。
        trace_weapon_build=os.environ.get('CODOL_TRACE_WEAPON_BUILD') == '1',
    )
    if online:
        from ..online.channel import PlayerChannel
        args.player_channel = PlayerChannel(PROJECT.parent, account, config.name)
    return args, observer
