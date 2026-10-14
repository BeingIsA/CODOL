# Version 2.6.9.7 research port; see docs/provenance.json.
"""领域层：把测试进度（等级/经验/解锁位）按结构写进档案。

输入是结构捕获（domain.playerdata_schema）导出的 schema JSON 与
assets/local-inventory/progression.json 种子；写入前断言 schema 身份，
写入后回读验证。写入对象是内存中的 playerdata（70 级测试档案）。
"""
import json, struct
from pathlib import Path


def seed(read, write, schema_path, config_path):
    """按 schema 偏移把等级、经验、版本与全部解锁位写入档案。

    ``read``/``write`` 为读写客户端内存的函数；目标地址是版本固定的
    playerdata 缓冲（0xdbff0c0 = 230682672 + 37272 附近的安全写入区）。
    返回写入字段明细（旧值/新值/偏移）与解锁位数量。
    """
    schema = json.loads(Path(schema_path).read_text(encoding='utf-8'))
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    # schema 身份断言：进度结构版本 19/3628788362、大小 8103，
    # 与 2.6.9.7 客户端捕获时一致，防止用错结构解释内存。
    assert [int(schema['header'][i], 16) for i in (0, 1)] == [19, 3628788362]
    assert schema['size'] == 8103
    fields = {p['name']: p for p in schema['structs'][schema['root_type'][1]]['properties']}
    address = 230682672 + 37272
    data = bytearray(read(address, 32764))
    changes = {}
    # 三个 32 位整数字段：等级、总经验、等级版本号。
    for name, value in [('rank', config['player_rank']), ('experience', config['player_experience']), ('rankVersion', config['rank_version'])]:
        field = fields[name]
        assert field['type'] == [0, 0]
        offset = schema['offsets'][field['offset']]
        changes[name] = {'before': struct.unpack_from('<I', data, offset)[0], 'value': value, 'offset': offset}
        struct.pack_into('<I', data, offset, value)
    # unlockFlags 是 4096 位的位域（定长字节数组），全部置 1 表示全解锁。
    flags = fields['unlockFlags']
    assert flags['type'][0] == 6
    array = schema['indexed_arrays'][flags['type'][1]]
    assert array[:3] == [4096, 2, 0]
    for i in range(array[0]):
        bit = schema['offsets'][flags['offset'] + i]
        data[bit // 8] |= 1 << bit % 8
    write(address, bytes(data))
    assert read(address, len(data)) == bytes(data)
    return {'fields': changes, 'unlock_flags': array[0], 'written_and_verified': True}
