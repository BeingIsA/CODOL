# Version 2.6.9.7 research port; see docs/provenance.json.
"""基础设施层：文件落盘的“链接优先、复制兜底”工具。"""
import errno
import os
import shutil


def link_or_copy(source, target):
    """把 ``source`` 放到 ``target``：优先硬链接，跨卷时退化为复制。

    客户端副本需要带入大量资源文件（fastfile、贴图等），硬链接在同一磁盘上
    几乎零开销；只有跨卷等无法链接的场景才真正复制字节。
    返回 'link' 或 'copy' 表示实际采用的手段；目标已存在时复制路径会失败。
    """
    try:
        os.link(source, target)
        return 'link'
    except OSError as exc:
        # errno.EXDEV（跨设备）与 winerror 17（卷不支持硬链接）是可回退场景，
        # 其他错误（如目标已存在）按原样抛出。
        if exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17:
            raise
    created = False
    try:
        # 'xb' 排他创建：绝不覆盖已存在的目标文件。
        with open(source, 'rb') as src, open(target, 'xb') as dst:
            created = True
            shutil.copyfileobj(src, dst, 1024 * 1024)
        return 'copy'
    except BaseException:
        if created:
            os.unlink(target)
        raise
