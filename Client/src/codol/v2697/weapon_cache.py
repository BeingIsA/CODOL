"""修复 2.6.9.7 首次武器缓存初始化重复清零导致双持切枪失败。

0x482110 先清空冷缓存，再把 WeaponDef+0x10 的第一人称手臂模型写入缓存；
0x482199 的第二次 memset 随即抹掉它。创建函数收到零模型便直接返回，
首次切到双持武器时左手对象尚不存在，更新检查报告 178。

方案参考旧项目 server2697/weapon_view_cache2697.py，本项目独立实现和校验。
仅改独立运行副本的重复清零调用，保留首次清零、模型加载、就绪标志与错误检查。
来源哈希及现场证据见 docs/切枪178错误定位.md；运行不依赖旧项目。
"""


def preserve_initial_model(raw, pe):
    """校验初始化顺序后移除第二次清零，返回可追溯补丁记录。

    raw 必须是已通过整镜像哈希校验的可写副本。三处前置条件全部满足才修改；
    任一字节不同即拒绝，避免版本错配或对已修改镜像重复应用。
    """
    guards = (
        (0x482156, bytes.fromhex('68240100006a0053e89de76c0083c40c')),
        (0x482182, bytes.fromhex('8b47108903')),
        (0x482199, bytes.fromhex('e862e76c00')),
    )
    for address, expected in guards:
        offset = pe.get_offset_from_rva(address - 0x400000)
        if raw[offset:offset + len(expected)] != expected:
            raise ValueError('首次武器缓存修复：版本字节不匹配 ' + hex(address))
    offset = pe.get_offset_from_rva(0x82199)
    raw[offset:offset + 5] = b'\x90' * 5
    return {'rva': '0x82199', 'before': guards[-1][1].hex(), 'after': '9090909090',
            'purpose': '保留首次武器缓存初始化已写入的手臂模型，避免双持左手创建被跳过'}
