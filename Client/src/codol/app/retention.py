"""应用层：历史会话客户端副本的自动回收。

每次运行都会在 .lobby/<会话>/runtime 生成一份完整客户端副本（约 2GB；
其中大部分资源文件与 assets 内解码缓存共享硬链接，实际新增字节只有
补丁 EXE 与少量 DLL）。会话结束后 runtime 不再有任何用途——证据都在
会话根目录的小 JSON 里；不回收则目录随运行次数无限累积。

回收规则（对齐 AGENTS.md 的谨慎约束）：
- 只在持有单会话锁时调用（由 lifecycle 保证，clean/launch/stage 同样）；
- 只处理 .lobby 下一层、名字形如 8 位十六进制会话 id 的目录；
- 只删除其中的 runtime/ 子目录，证据 JSON 与其他产物全部保留；
- data/active.json 仍指向的目录跳过——那是异常退出的残留现场；
- 单个目录删除失败只记录跳过，不中断其余回收，也不阻塞启动。
"""
import json
import re
import shutil
import stat
from pathlib import Path
from ..paths import DATA, RUNS

# 会话目录名 = uuid4().hex[:8]，见 app.context.prepare。
SESSION_ID = re.compile(r'[0-9a-f]{8}')


def _same_path(left, right):
    """Windows 路径不区分大小写，比较前先统一小写。"""
    return str(left).lower() == str(right).lower()


def _force_rmtree(folder):
    """删除目录树；只读属性文件先去只读再重试一次。

    副本内文件大多从 assets/原始目录硬链接而来，若带只读属性，
    Windows 上直接删除会报 PermissionError。
    """

    def relax(function, path, exc):
        Path(path).chmod(stat.S_IWRITE)
        function(path)

    shutil.rmtree(folder, onexc=relax)


def prune_runtimes(keep=None):
    """删除除 keep 外所有历史会话的 runtime/，返回 (回收数, [跳过说明])。

    ``keep`` 为需要完整保留的会话目录（当前活动会话），可为 None。
    调用方必须已持有单会话锁。
    """
    pruned, skipped = 0, []
    root = RUNS.resolve()
    if not root.is_dir():
        return pruned, skipped
    keep = keep.resolve() if keep is not None else None
    protected = None
    active = DATA / 'active.json'
    if active.exists():
        try:
            protected = Path(json.loads(active.read_text(encoding='utf-8'))['run']).resolve()
        except (OSError, ValueError, KeyError):
            protected = None
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir() or not SESSION_ID.fullmatch(candidate.name):
            continue
        resolved = candidate.resolve()
        if keep is not None and _same_path(resolved, keep):
            continue
        if protected is not None and _same_path(resolved, protected):
            skipped.append(candidate.name + '：active.json 仍指向它，保留现场')
            continue
        runtime = resolved / 'runtime'
        if not runtime.is_dir():
            continue
        try:
            _force_rmtree(runtime)
            pruned += 1
        except OSError as exc:
            skipped.append(f'{candidate.name}：{exc}')
    return pruned, skipped
