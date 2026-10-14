"""应用层：单会话互斥锁。

同一时刻只允许一个大厅会话：锁文件位于 data/session.lock，用 Windows
文件区间锁（msvcrt.locking）实现跨进程互斥。重复启动直接报错退出，
提示先结束现有会话。
"""
import msvcrt
from contextlib import contextmanager
from ..paths import DATA


@contextmanager
def exclusive_session():
    """以独占锁进入会话；重复启动抛 RuntimeError，退出时自动释放。"""
    DATA.mkdir(exist_ok=True)
    with (DATA / 'session.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('已有大厅会话正在运行，请先退出。') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
