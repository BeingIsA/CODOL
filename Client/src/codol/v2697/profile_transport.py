"""战斗传输档案头与本地保存格式的版本适配；不改变档案正文或校验规则。"""
import struct


def local_save_blob(blob, fixture, meta):
    """仅将已知传输头归一为原本地头；未知头保持原样，由存档校验器拒绝。

入场桥写入 schema 身份供原生 stats 发送，磁盘档案仍遵循既有零头格式。
不修改客户端内存，不绕过 profile_store 对大小、结构与字段的校验。
"""
    if (meta['schema_version'], meta['schema_checksum']) == (233, 0x70cd4cc5):
        if blob[:8] == struct.pack('<II', 233, 0x70cd4cc5):
            return fixture[:8] + blob[8:]
    return blob
