"""应用层：各命令的执行体（stop / check / clean / stage / launch / smoke）。

由入口层（cli）解析参数后调用。本模块负责：
- ``request_stop``   按 data/active.json 定位自己的会话并写停止请求；
- ``clean_runtimes`` 回收全部历史会话的客户端副本；
- ``run_stage``      只准备客户端副本，不启动游戏；
- ``run_session``    启动完整调试会话（launch/smoke 共用），结束后
                     汇总成功/失败并写 summary.json。
"""
import json
from pathlib import Path
from ..paths import DATA, RUNS
from .context import prepare
from .retention import prune_runtimes
from .session_lock import exclusive_session
from ..domain.readiness import failures


def _reclaim_old_runtimes():
    """回收历史会话副本并打印摘要；无事可做时保持安静。"""
    pruned, skipped = prune_runtimes()
    if pruned:
        print(f'已回收 {pruned} 个历史会话的客户端副本（证据 JSON 保留）。', flush=True)
    for note in skipped:
        print('跳过历史会话 ' + note, flush=True)


def clean_runtimes():
    """回收全部历史会话的客户端副本（codol clean 命令体），返回退出码。"""
    with exclusive_session():
        _reclaim_old_runtimes()
    print('历史会话副本回收完成。', flush=True)
    return 0


def request_stop():
    """请求结束当前活动会话，返回进程退出码。

    活动会话标记 data/active.json 记录会话目录；目录无效时直接报错，
    防止把停止请求写进意料之外的位置。
    """
    active = DATA / 'active.json'
    if not active.exists():
        print('没有正在运行的大厅会话。')
        return 0
    document = json.loads(active.read_text(encoding='utf-8'))
    run = Path(document['run']).resolve()
    if run.parent != RUNS.resolve() or not run.is_dir():
        raise ValueError('当前会话目录无效。')
    (run / 'stop.request').write_text('stop', encoding='ascii')
    print('已请求保存存档并结束大厅会话。')
    return 0


def run_stage(config, command, seconds):
    """在会话锁内准备客户端副本（codol stage 命令体），返回退出码。"""
    with exclusive_session():
        args, _ = prepare(config, command == 'smoke', seconds)
        print('本次日志：' + str(args.run_directory), flush=True)
        _reclaim_old_runtimes()
        args.run_directory.mkdir()
        stage = args.run_directory / 'runtime'
        stage.mkdir()
        exe, report = args.stage_client(stage, args)
        (args.run_directory / 'stage.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('客户端副本准备完成：' + str(exe), flush=True)
        return 0


def run_session(config, command, seconds):
    """启动完整调试会话（codol launch / smoke 命令体），返回退出码。

    流程：装配 → 打开游戏（调试会话）→ 结果归类 → 写 summary.json。
    活动会话标记在会话期间写入 data/active.json，供 stop 命令定位。
    """
    with exclusive_session():
        args, observer = prepare(config, command == 'smoke', seconds)
        print('本次日志：' + str(args.run_directory), flush=True)
        _reclaim_old_runtimes()
        from ..v2697.session import run
        print('正在启动游戏；Windows 可能要求确认运行保护组件。', flush=True)
        active = DATA / 'active.json'
        pending = DATA / 'active.pending'
        pending.write_text(json.dumps({'run': str(args.run_directory)}), encoding='utf-8')
        pending.replace(active)
        try:
            report = run(args)
        finally:
            active.unlink(missing_ok=True)
        errors = failures(report)
        if not observer.ready:
            errors.append('尚未观测到大厅就绪。')
        summary = {'lobby_ready': observer.ready, 'state': observer.last,
                   'errors': errors, 'run': str(args.run_directory),
                   'verification': 'client-memory; visual acceptance separate'}
        (args.run_directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        if errors:
            print('运行未通过：' + '\n'.join(errors), flush=True)
            return 1
        print('大厅会话结束，存档已保留。', flush=True)
        return 0
