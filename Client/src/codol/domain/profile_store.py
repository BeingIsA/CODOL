# Version 2.6.9.7 research port; see docs/provenance.json.
"""领域层：本地档案的版本校验、备份恢复与原子保存。

档案是 32764 字节的二进制 ``playerdata``。本模块负责：
- 以版本固定的基准（assets/local-profile-loadout，含 schema 身份）校验结构；
- 读档时损坏档优先从 ``.previous.json`` 备份恢复，仍失败则报错，
  绝不静默覆盖坏档；
- 保存走临时文件 + ``os.replace`` 原子替换，成功保存前先滚动备份。

本模块仅为离线测试档案服务，不做账号/网络语义。
"""
import base64, hashlib, json, os, struct, time
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4)
def _profile_layout(source_schema, version, checksum, size):
    """解析 schema JSON，返回档案关键字段的 (格式, 偏移) 描述。

    ``source_schema`` 指向 assets/schemas/playerdata-schema.json（由
    profile.json 元数据携带）。schema 与元数据的版本/校验和/大小必须
    三者一致，否则视为不支持的档案结构直接拒绝。

    返回三元组：当前角色索引字段、12 个角色槽字段、主武器基准字段。
    """
    schema = json.loads(Path(source_schema).read_text(encoding='utf-8'))
    identity = (int(schema['header'][0], 0), int(schema['header'][1], 0), schema['size'])
    if identity != (version, checksum, size):
        raise ValueError('Captured profile schema does not match metadata')

    def field(path):
        # 把 'a.b.0.c' 形式的字段路径换算成 (类型种类, 表索引, 展平序号)。
        kind, index = schema['root_type']
        flat = 0
        for component in path.split('.'):
            if kind == 5:  # 结构体：按属性偏移推进
                prop = next((p for p in schema['structs'][index]['properties'] if p['name'] == component))
                flat += prop['offset']
                kind, index = prop['type']
            elif kind == 6:  # 定长数组：按元素大小推进
                count, kind, index, _ = schema['indexed_arrays'][index]
                element = int(component)
                if not 0 <= element < count:
                    raise ValueError('Profile field index out of range')
                flat += element * (schema['structs'][index]['header'][4] if kind == 5 else 1)
            else:
                raise ValueError('Unsupported profile field path')
        return (kind, index, flat)

    def integer(path):
        # 整数字段：再经 offsets 表映射到 buffer 内的字节偏移。
        kind, _, flat = field(path)
        if kind not in (0, 1, 9):
            raise ValueError('Profile field is not an integer')
        fmt = {0: '<i', 1: '<B', 9: '<h'}[kind]
        offset = schema['offsets'][flat]
        if not 8 <= offset or offset + struct.calcsize(fmt) > size:
            raise ValueError('Profile field is outside the schema buffer')
        return (fmt, offset)

    kind, array, _ = field('characterClasses')
    if kind != 6:
        raise ValueError('Character classes are not an indexed array')
    count = schema['indexed_arrays'][array][0]
    if count != 12:
        raise ValueError('Unexpected character class count')
    return (integer('currentCharacterIndex'), tuple((integer(f'characterClasses.{index}.slots.0') for index in range(count))), integer('weaponClasses.0.primaryBase'))


def validate(blob, fixture, meta):
    """校验档案二进制：大小/头部与基准一致、结构版本固定、字段合法。

    返回 {character_index, character, primary, sha256}；任何不一致抛
    ``ValueError``。
    """
    if len(blob) != len(fixture) or blob[:8] != fixture[:8]:
        raise ValueError('Local profile size/header mismatch')
    if (meta['schema_version'], meta['schema_checksum'], meta['schema_size']) != (233, 1892502725, 9610):
        raise ValueError('Unsupported local profile schema')
    index_field, characters, primary_field = _profile_layout(meta['source_schema'], meta['schema_version'], meta['schema_checksum'], meta['schema_size'])

    def read(field):
        fmt, offset = field
        return struct.unpack_from(fmt, blob, offset)[0]

    index = read(index_field)
    if not 0 <= index < len(characters):
        raise ValueError('Invalid current character index')
    character = read(characters[index])
    if character <= 0:
        raise ValueError('Selected character is missing')
    return {'character_index': index, 'character': character, 'primary': read(primary_field), 'sha256': hashlib.sha256(blob).hexdigest()}


def decode(path, fixture, meta):
    """读取并校验一份 JSON 存档，返回 (二进制, 校验信息)。

    存档行格式：base64 档案 + 保存时校验和。版本或校验和不符即抛错。
    """
    row = json.loads(path.read_text(encoding='utf-8'))
    if row['format'] != 1 or row['schema_version'] != meta['schema_version'] or row['schema_checksum'] != meta['schema_checksum']:
        raise ValueError('Saved profile version mismatch')
    blob = base64.b64decode(row['profile_base64'], validate=True)
    info = validate(blob, fixture, meta)
    if info['sha256'] != row['sha256']:
        raise ValueError('Saved profile checksum mismatch')
    return (blob, dict(info, path=str(path), saved_at=row['saved_at']))


def load(path, fixture, meta):
    """加载用户存档：主档 → 备份 → 全新基准档 的回退链。

    - 主档/备份都存在但都无效：抛出带全部原因的 ``ValueError``，不覆盖；
    - 没有任何存档：返回基准 fixture 作为新档（new_profile=True）。
    """
    errors = []
    for candidate in (path, path.with_suffix('.previous.json')):
        if not candidate.exists():
            continue
        try:
            blob, info = decode(candidate, fixture, meta)
            return (blob, dict(info, recovered_from_backup=candidate != path, ignored_errors=errors))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError('No valid saved profile: ' + '; '.join(errors))
    return (fixture, dict(validate(fixture, fixture, meta), path=None, new_profile=True))


def save(path, blob, fixture, meta, run):
    """原子保存档案：先滚动备份旧档，再写临时文件并替换。

    备份仅在旧档能通过校验时滚动，避免用坏档覆盖唯一好备份。
    返回校验信息（含保存时间与路径），写入失败抛异常且不动旧档。
    """
    info = validate(blob, fixture, meta)
    row = dict(format=1, schema_version=meta['schema_version'], schema_checksum=meta['schema_checksum'], saved_at=time.time(), run=str(run), **info, profile_base64=base64.b64encode(blob).decode('ascii'))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            decode(path, fixture, meta)
        except (ValueError, KeyError, TypeError):
            pass
        else:
            previous = path.with_suffix('.previous.json')
            pending = previous.with_suffix('.tmp')
            pending.write_bytes(path.read_bytes())
            os.replace(pending, previous)
    pending = path.with_suffix('.tmp')
    with pending.open('w', encoding='utf-8') as file:
        json.dump(row, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(pending, path)
    return dict(info, path=str(path), saved_at=row['saved_at'])
