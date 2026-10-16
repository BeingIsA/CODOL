"""2.6.9.7 本机开发战斗档案适配，供 NativeSupervisor 在启动前发布。

这是显式的测试进度，不是正式账号或成长系统：每局独立生成，不接受客户端
上传等级/货币，不回写用户存档。偏移核对自 bdcb74b9 的 playersecuredata
结构捕获（19/d84aea8a、8103 字节），编码依据原生 codol_profile_storage.c。
正式账号接入时应替换此入口，由账号仓库提供权威档案。
"""
import hashlib
import struct
import zlib


def encode_development_profile():
    """生成固定开发档案：70级、测试经验、解锁位；附原生 CRC 后 zlib 压缩。"""
    record = bytearray(32764)
    struct.pack_into('<II', record, 0, 19, 0xd84aea8a)
    for offset, value in ((4883, 70), (4840, 3582550), (4887, 2)):
        struct.pack_into('<I', record, offset, value)
    # schema 的 4096 个解锁位分布于两个区段，不能误当成连续 512 字节。
    for start, end in ((39672, 40696), (60472, 63544)):
        for bit in range(start, end):
            record[bit // 8] |= 1 << (bit % 8)
    payload = struct.pack('<I', zlib.crc32(record)) + record
    return zlib.compress(payload)


def publish_development_profiles(directory, players):
    """只为冻结成员发布十进制 UID 文件；独占新建，拒绝覆盖任何既有档案。"""
    payload = encode_development_profile()
    identities = [int(player['native_id']) for player in players]
    if len(set(identities)) != len(identities) or any(not 0 < uid < 2**64 for uid in identities):
        raise ValueError('开发档案成员 UID 非法或重复。')
    for uid in identities:
        with (directory / f'mpdata_s.{uid}').open('xb') as output:
            output.write(payload)
    return {'development_only': True, 'schema': '19/d84aea8a',
            'uids': identities, 'compressed_sha256': hashlib.sha256(payload).hexdigest()}
