"""安装包资源目录索引：保留包内真实路径，为仅有高清版的贴图建立读取别名。

旧实验 runtime_image_index2697.py 曾补充 hires 映射；这里直接纳入资源层，
路径由当前配置提供，不依赖旧启动器或固定安装目录。普通 main 资源优先，
hires 仅作为候选回退；只改变资源查找，不替换内容或放宽文件门。
"""
import re
from pathlib import Path


_ENTRY = re.compile(
    rb'(?im)^((?:main[/\\][^\x00-\x1f\x7f-\xff:<>"|?*]+?\.(?:iwi|mp3|wav)'
    rb'|main[/\\]models[/\\][a-z0-9_/\\-]+'
    rb'|client_shipretail[/\\][^\x00-\x1f\x7f-\xff:<>"|?*]+?\.ff'
    rb'|hires[/\\]images[/\\][^\x00-\x1f\x7f-\xff:<>"|?*]+?\.iwi))\r?$')


def build_index(archives):
    """扫描 IFS 前 1MB 清单；拒绝路径逃逸，返回目标名到真实包内路径的候选表。"""
    index, high_resolution = {}, []
    for archive in archives:
        with archive.open('rb') as stream:
            prefix = stream.read(1024 * 1024)
        for match in _ENTRY.finditer(prefix):
            name = match.group(1).decode('ascii').replace('\\', '/')
            if Path(name).is_absolute() or '..' in Path(name).parts:
                continue
            if name.lower().startswith('hires/images/'):
                high_resolution.append(('main/' + name.split('/', 1)[1], archive, name))
            else:
                index.setdefault(name.lower(), []).append((archive, name))
    for target, archive, source in high_resolution:
        index.setdefault(target.lower(), []).append((archive, source))
    return index
