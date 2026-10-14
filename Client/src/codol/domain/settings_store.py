# Version 2.6.9.7 research port; see docs/provenance.json.
"""领域层：原生画质/按键设置的捕获、恢复与稳定写入检测。

设置文件是客户端自己写的 ``players/config_mp.cfg``，内容为不透明字节。
本模块只做字节级搬运（存档库 + 副本恢复），绝不解码或凭空造设置：
画质应用逻辑始终留在原生引擎里，装备/档案数据由其他模块负责。

存档库位于 assets/offline-settings（首次由历史干净运行捕获而来），
app.context 会把它重定向到按用户名划分的 data 目录。
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import time
import uuid
from ..paths import ASSETS as root
# 存档库根目录；prepare 阶段会按用户重设为 data/<摘要>/settings。
store = root / 'offline-settings'


def _atomic(path, data):
    """临时文件 + os.replace 的原子写入；无论成败都清理临时文件。"""
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        pending.write_bytes(data)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _read_native(source):
    """稳定地读取客户端写出的设置文件，读取期间内容变化则放弃。

    通过两次 stat + 两次读取比对大小/修改时间/内容，排除客户端正在
    写入一半时读到坏数据。任何异常返回 None 交由调用方重试。
    """
    try:
        before = source.stat()
        if not 16 <= before.st_size <= 1024 * 1024:
            return None
        data = source.read_bytes()
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        if len(data) != after.st_size or data != source.read_bytes():
            return None
        return data
    except OSError:
        return None


def _persist(data, run):
    """把一份设置字节存入存档库并更新 manifest，返回记录报告。

    内容哈希不变时直接复用旧记录；config_mp.cfg 变化时滚动保留
    上一版（config_mp.previous.cfg）。
    """
    digest = hashlib.sha256(data).hexdigest()
    manifest = store / 'manifest.json'
    if manifest.exists():
        old = json.loads(manifest.read_text())
        if old['sha256'] == digest:
            return old
    store.mkdir(parents=True, exist_ok=True)
    name = 'config-' + digest + '.cfg'
    _atomic(store / name, data)
    target = store / 'config_mp.cfg'
    if target.exists() and target.read_bytes() != data:
        shutil.copyfile(target, store / 'config_mp.previous.cfg')
    _atomic(target, data)
    report = dict(format=2, client_version='2.6.9.7', source_run=run.name, saved_at=time.time(), bytes=len(data), sha256=digest, file=name, encoding='native-client-config')
    _atomic(manifest, json.dumps(report, indent=2).encode())
    return report


def capture(run):
    """历史导入用：只从"干净结束"的历史运行目录捕获设置。

    要求该运行无错误、保护组件确认清理、无引擎错误/崩溃，否则返回
    None（宁可不恢复，也不引入脏设置）。
    """
    result = json.loads((run / 'result.json').read_text())
    if result.get('error') or result.get('guard_cleanup', {}).get('engine_close_status') != 0:
        return None
    if any((e.get('milestone') == 'Engine error' or e.get('code') == '0xc0000005' for e in result.get('exceptions', []))):
        return None
    data = _read_native(run / 'runtime/players/config_mp.cfg')
    return _persist(data, run) if data is not None else None


def stage(runtime):
    """把存档库中的设置恢复进客户端副本，返回恢复报告。

    存档库为空时先尝试从历史干净运行捕获一次（一次性导入路径）。
    恢复时严格核对文件名单一性、版本与哈希，任何不符都会断言失败。
    """
    if not (store / 'manifest.json').exists():
        for run in sorted(root.glob('restricted-candidate-20261007-*'), reverse=True):
            if (run / 'result.json').is_file() and capture(run):
                break
    if not (store / 'manifest.json').exists():
        return {'restored': False}
    report = json.loads((store / 'manifest.json').read_text())
    name = report.get('file', 'config_mp.cfg')
    # 防路径逃逸：记录中的文件名必须是不含任何分隔符的纯文件名。
    assert Path(name).name == name and '/' not in name and ('\\' not in name)
    data = (store / name).read_bytes()
    assert report['client_version'] == '2.6.9.7'
    assert hashlib.sha256(data).hexdigest() == report['sha256']
    target = runtime / 'players/config_mp.cfg'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    result = {'restored': True, **report}
    (runtime.parent / 'settings-restored.json').write_text(json.dumps(result, indent=2))
    return result


def launch_arguments(arguments, restored):
    """按设置恢复情况调整启动参数。

    诊断启动器默认强制窗口化（+set r_fullscreen 0）；当副本恢复了
    用户自己的原生设置时移除该强制项，把分辨率/窗口决定权还给设置文件。
    """
    result = list(arguments)
    if restored:
        for i in range(len(result) - 3, -1, -1):
            if result[i:i + 3] == ['+set', 'r_fullscreen', '0']:
                del result[i:i + 3]
    return result


class Watcher:
    """轮询副本内的设置文件，连续两次读到相同新内容才落盘。

    两次一致才算"客户端写完了"，避免把半截文件存进库；崩溃前也会用
    ``finish()`` 做最后一次尽力保存。
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.source = runtime / 'players/config_mp.cfg'
        self.last = _read_native(self.source)
        self.pending = None
        self.next = 0

    def poll(self):
        """每秒至多检查一次；发现稳定新内容时持久化并返回报告。"""
        now = time.monotonic()
        if now < self.next:
            return None
        self.next = now + 1
        data = _read_native(self.source)
        if data is None or data == self.last:
            self.pending = None
            return None
        if data != self.pending:
            self.pending = data
            return None
        report = _persist(data, self.runtime.parent)
        self.last = data
        self.pending = None
        return report

    def finish(self):
        """会话收尾时的最后一次保存：有未落盘的新内容即持久化。"""
        data = _read_native(self.source)
        if data is not None and data != self.last:
            report = _persist(data, self.runtime.parent)
            self.last = data
            return report
        return None
