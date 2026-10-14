"""领域层：大厅就绪的观察与运行结果的成功/失败判定。

设计原则：就绪状态只从受管客户端的内存观察得出，绝不因为“进程创建了”
或“窗口出现了”就推断成功。观察地址仅适用于 2.6.9.7，由版本适配层传入
调用；本模块本身不依赖版本地址常量。
"""
import json
import struct
import time
from pathlib import Path


class LobbyObserver:
    """轮询客户端内存中的大厅状态，连续稳定三秒才判定就绪。

    观察项（地址由调用方传入读内存函数）：
    - 原生 UI movie 指针非空、UI 激活标记、玩家数据就绪标记；
    - 仓库物品数 > 0、装备加载状态 == 4、选中角色 > 0。

    同时负责：
    - 检测 ``stop.request`` 文件实现外部停止请求；
    - 每次采样原子写入 ``status.json``，供会话目录留存证据。

    可调用对象签名 ``observer(read, process, run) -> bool``：
    返回 True 表示应当结束会话（就绪即停的 smoke，或收到停止请求）。
    """

    def __init__(self, stop_when_ready=False):
        self.stop_when_ready = stop_when_ready
        self.next_poll = 0          # 下次允许采样的单调时钟时间（0.5s 节流）
        self.ready_since = None     # 状态首次全部有效的时间；无效即清零
        self.ready = False          # 连续三秒有效后的最终就绪标记
        self.stop_requested = False # 是否看到过 stop.request
        self.last = {}              # 最近一次采样结果

    def __call__(self, read, process, run):
        # 停止请求优先于一切：文件存在即请求保存并结束本次会话。
        if (Path(run) / 'stop.request').exists():
            self.stop_requested = True
            return True
        now = time.monotonic()
        if now < self.next_poll:
            return False
        self.next_poll = now + 0.5
        try:
            word = lambda address: struct.unpack('<I', read(address, 4))[0]
            state = {
                'movie': word(0xdba969c),
                'ui_activated': read(0x747c713, 1)[0],
                'player_data_ready': read(0x7454ad5, 1)[0],
                'inventory_count': word(0x7454ac4),
                'loadout_state': word(0x7474f00),
                'character': word(0xd27150),
            }
        except OSError:
            # 进程内存暂不可读（尚未初始化或正在退出）：重置稳定窗口。
            self.ready_since = None
            return False
        # 六项指标须同时满足；任一不满足则重新开始计三秒。
        valid = bool(state['movie'] and state['ui_activated'] and state['player_data_ready']
                     and state['inventory_count'] > 0 and state['loadout_state'] == 4
                     and state['character'] > 0)
        if not valid:
            self.ready_since = None
        elif self.ready_since is None:
            self.ready_since = now
        if valid and now - self.ready_since >= 3 and not self.ready:
            self.ready = True
            print('大厅状态已就绪：角色、仓库和界面均已加载。', flush=True)
        # 采样结果原子写入 status.json：先写 .pending 再替换。
        self.last = dict(state, lobby_ready=self.ready, sampled_at=time.time())
        target = Path(run) / 'status.json'
        pending = target.with_suffix('.pending')
        pending.write_text(json.dumps(self.last, indent=2), encoding='utf-8')
        pending.replace(target)
        return self.stop_when_ready and self.ready


def failures(report):
    """把会话结果报告归类为面向用户的错误列表；空列表代表通过。

    检查项：宿主异常、异常退出码、档案/设置保存失败、非正常停止原因、
    引擎错误与错误弹窗、保护组件清理确认。任何一项缺失或不符都算失败，
    避免“只看到窗口”被误判为成功。
    """
    errors = []
    if report.get('error'):
        errors.append(report['error'])
    if report.get('exit_code') not in (None, '0x0'):
        errors.append('客户端异常退出：' + report['exit_code'])
    for field in ('profile_save_error', 'settings_save_error'):
        if report.get(field):
            errors.append(field + ': ' + report[field])
    reason = report.get('stopped_reason', '')
    if reason and reason != 'Bounded observation time reached':
        errors.append(reason)
    for row in report.get('exceptions', []):
        if row.get('milestone') == 'Engine error':
            errors.append(str(row.get('argument_strings', row)))
        elif row.get('milestone', '').startswith('MessageBox') and row.get('dialog_kind') != 'confirmation':
            errors.append(str(row.get('message', row)))
    if report.get('guard_cleanup', {}).get('engine_close_status') != 0:
        errors.append('运行保护组件未确认正常清理。')
    return errors
