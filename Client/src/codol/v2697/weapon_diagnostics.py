"""2.6.9.7 切枪错误的只读现场采集。

地址来自已校验恢复镜像的 0x485b20 更新函数及 0x4824a0 创建函数。
只接受 178 错误的准确调用点；不吞掉错误、不修改武器或放宽进程隔离。
采集用于区分左右手对象创建失败与装备状态不一致，不把空对象等同于缺文件。
"""
import struct

# 仅在显式诊断模式启用；各点均为完整指令起始，预期字节用于防止版本误用。
BUILD_POINTS = (
    (0x4824d7, b'\x39\x58\x14', 'prerequisites'),
    (0x4827ee, b'\x85\xc0', 'hand eligibility'),
    (0x482a84, b'\x89\x07', 'object created'),
    (0x482bd7, b'\x5f', 'builder return'),
)


def capture_build_step(memory, frame, registers):
    """采集创建函数同一栈帧的输入和每手分支，关联创建前后状态。

    此处 EBP 属于 0x4824a0。每次只读固定大小字段；读失败由调用者记录，
    不能通过诊断修改调用参数或返回值。registers 是断点时的实际寄存器。
    """
    def uint(address):
        """读取一个原生整数，失败向上交给诊断边界处理。"""
        return struct.unpack('<I', memory(address, 4))[0]

    client, weapon = uint(frame + 0xc), uint(frame + 0x10)
    result = {'frame': hex(frame), 'client': hex(client), 'weapon': weapon,
              'input_models': [hex(uint(frame + offset)) for offset in (0x14, 0x18)],
              'hand_register': registers['ebx']}
    if not 0 < weapon < 700:
        return result
    definition = uint(0x187d888 + weapon * 4)
    result.update(selected=uint(client + 0x334), offhand=uint(client + 0x320),
                  flags=uint(client + 0x33c), definition=hex(definition),
                  dual=memory(definition + 0xc0d, 1)[0],
                  model=hex(uint(definition + 0x14)))
    result['equipped'] = [
        {'slot': slot, 'bytes': memory(client + 0x2d4 + slot * 5, 5).hex()}
        for slot in range(15) if uint(client + 0x298 + slot * 4) == weapon]
    return result


def capture_weapon_error(memory, stack_pointer, frame_pointer):
    """读取已知错误调用帧和武器对象，返回可写入 result.json 的有限快照。

    memory 为宿主的只读内存函数。调用点不匹配时返回 None；读失败时保留
    已获取的字段及错误，避免诊断失败覆盖原始引擎错误。所有偏移仅限 2.6.9.7。
    """
    result = {}

    def uint(address):
        """按小端读取一个原生 32 位字段，短读由外层统一记录。"""
        return struct.unpack('<I', memory(address, 4))[0]

    try:
        if uint(stack_pointer) != 0x485d42:
            return None
        result['call_site'] = '0x485d3d'
        # 当前断点在 Com_Error 第一条指令之前，EBP 仍属于武器更新函数。
        client = uint(frame_pointer - 0x40)
        weapon = uint(frame_pointer - 0x30)
        result.update(client_state=hex(client), weapon_index=weapon,
                      expected_last_hand=uint(frame_pointer - 0x38),
                      local_client=uint(frame_pointer - 0x3c))
        if not 0 < weapon < 700:
            result['read_error'] = 'weapon index outside verified table capacity'
            return result
        definition = uint(0x187d888 + weapon * 4)
        result['definition'] = hex(definition)
        result['definition_dual_wield_byte'] = memory(definition + 0xc0d, 1)[0]
        result['definition_hand_models'] = [hex(uint(definition + offset))
                                            for offset in (0x14, 0x18)]
        result['selected_weapon'] = uint(client + 0x334)
        result['offhand_weapon'] = uint(client + 0x320)
        result['weapon_flags'] = uint(client + 0x33c)
        result['cached_weapon'] = uint(client + 0xbfab4)
        result['cached_dual_wield'] = uint(client + 0xbfab8)
        # 0x40bfe0 从 15 个装备槽中查找武器，返回每槽 5 字节的状态记录。
        result['matching_equipped_slots'] = [
            {'slot': slot, 'state_bytes': memory(client + 0x2d4 + slot * 5, 5).hex()}
            for slot in range(15) if uint(client + 0x298 + slot * 4) == weapon]
        selector = uint(0x7454998)
        if selector >= 4096:
            result['read_error'] = 'hand object selector outside diagnostic bound'
            return result
        hands = uint(0x1a65e0c + selector * 4) ^ 0x1342d439
        result['hands'] = [
            {'hand': name, 'object': hex(uint(hands + index * 0x18)),
             'animation_tree': hex(uint(hands + index * 0x18 + 4))}
            for index, name in enumerate(('right', 'left'))]
    except (OSError, ValueError, struct.error, IndexError) as exc:
        result['read_error'] = str(exc)
    return result
