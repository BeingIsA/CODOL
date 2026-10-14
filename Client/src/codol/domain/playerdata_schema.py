# Version 2.6.9.7 research port; see docs/provenance.json.
"""领域层：从客户端内存捕获 playerdata 结构定义（schema）。

客户端在初始化进度档案时会构造一份"结构定义"对象（描述字段名、类型、
数组与字节偏移）。本模块把它从内存完整导出为 JSON，供后续按结构读写
档案字段（progression.seed 与 profile_store 的偏移换算都依赖它）。

导出内容写入会话目录 ``secure-schema/``：
- playerdata-schema.json —— 解析后的结构定义；
- schema-raw-header.json / asset-pages*.bin —— 原始头部与相关内存页，
  供排错时核对。
"""
import json, struct


def schema(read, address, folder):
    """从 ``address`` 处的 定义对象 导出结构，返回摘要信息。

    ``read`` 为读客户端内存的函数。头部布局（16 个 32 位字）：
    索引 2/3=枚举数量/表，4/5=结构体数量/表，6/7=定长数组，8/9=枚举数组，
    10/11=根类型，12=buffer 大小，13/14=偏移表长度/地址。
    """
    folder.mkdir(exist_ok=True)

    def words(a, n):
        return struct.unpack('<' + 'I' * n, read(a, 4 * n))

    def text(a):
        # 读一个以 NUL 结尾的 UTF-8 名称；内存不可读时退化为 @地址。
        if not a:
            return ''
        try:
            return read(a, 256).split(b'\x00')[0].decode('utf-8', errors='replace')
        except OSError:
            return f'@{a:08x}'

    h = words(address, 16)
    raw_meta = {'address': hex(address), 'header': [hex(x) for x in h], 'tables': {}}
    # 先留存原始头部与各表指针，便于离线核对。
    for label, index in [('enums', 3), ('structs', 5), ('indexed_arrays', 7), ('enumed_arrays', 9), ('offsets', 14)]:
        if h[index] > 65536:
            try:
                raw_meta['tables'][label] = {'address': hex(h[index]), 'words': [hex(x) for x in words(h[index], 64)]}
            except OSError as exc:
                raw_meta['tables'][label] = {'error': str(exc)}
    (folder / 'schema-raw-header.json').write_text(json.dumps(raw_meta, indent=2))
    # 把定义对象所在的 64KB 内存页及邻页原样导出，作为解析证据。
    anchors = {address & ~65535}
    anchors.update((h[i] & ~65535 for i in (3, 5, 7, 9, 14) if h[i] > 65536))
    regions = []
    for anchor in sorted(anchors):
        for base in range(max(0, anchor - 65536), anchor + 196608, 65536):
            if any((x['base'] == base for x in regions)):
                continue
            try:
                block = read(base, 65536)
                name = f'asset-page-{base:08x}.bin'
                (folder / name).write_bytes(block)
                regions.append({'base': base, 'file': name, 'size': len(block)})
            except OSError:
                pass
    (folder / 'asset-pages.json').write_text(json.dumps(regions, indent=2))
    out = {'address': hex(address), 'header': [hex(x) for x in h], 'root_type': list(h[10:12]), 'size': h[12]}
    # 合理性上限：防止被破坏的定义导致超长读取。
    assert 0 < h[12] <= 32764
    for count in (h[2], h[4], h[6], h[8]):
        assert count < 4096
    enums = []
    for i in range(h[2]):
        a = words(h[3] + 12 * i, 3)
        assert a[0] < 16384
        rows = []
        for j in range(a[0]):
            name, index = words(a[2] + 8 * j, 2)
            rows.append({'name': text(name), 'index': index})
        enums.append({'header': list(a), 'entries': rows})
    structs = []
    for i in range(h[4]):
        a = words(h[5] + 20 * i, 5)
        assert a[0] < 8192
        props = []
        for j in range(a[0]):
            name, kind, index, offset, extra1, extra2 = words(a[1] + 24 * j, 6)
            props.append({'name': text(name), 'type': [kind, index], 'offset': offset, 'extra': [extra1, extra2]})
        structs.append({'header': list(a), 'properties': props})
    out.update(enums=enums, structs=structs, indexed_arrays=[list(words(h[7] + 16 * i, 4)) for i in range(h[6])], enumed_arrays=[list(words(h[9] + 16 * i, 4)) for i in range(h[8])])
    if 0 < h[13] < 65536 and h[14] > 65536:
        out['offsets'] = list(words(h[14], h[13]))
    (folder / 'playerdata-schema.json').write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    # 顺带留存捕获时刻的档案原始内容，供与填充后对比。
    try:
        (folder / 'profile-before.bin').write_bytes(read(230682672 + 144, 32764))
    except OSError:
        pass
    return {'schema': str(folder / 'playerdata-schema.json'), 'size': h[12], 'root_type': out['root_type'], 'counts': {'enums': h[2], 'structs': h[4], 'indexed_arrays': h[6], 'enumed_arrays': h[8]}}
