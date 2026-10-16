"""M2 副本安装：原生安全队列泵、DLL 哈希与服务器身份投影。

借鉴旧 run_room_match2697.py 的 59d070 安全队列调度，重新限定为房间桥接。
所有补丁都先验证原字节；仅修改本次副本，不改变原始 EXE 或共享缓存。
"""
import hashlib
import shutil
import struct
import pefile
from ..paths import PROJECT
from .room_mailbox import RoomMailbox


def install(exe, stage, args):
    """追加独立可执行节，把周期 UI 回调转成有限房间泵。"""
    dll = PROJECT / "native/build/room_bridge2697.dll"
    if not dll.is_file():
        raise ValueError("缺少 M2 房间桥接，请先运行 Client/tools/build_room_bridge.py。")
    shutil.copy2(dll, stage / dll.name)
    raw = bytearray(exe.read_bytes())
    pe = pefile.PE(data=raw)
    base = pe.OPTIONAL_HEADER.ImageBase
    rva = pe.OPTIONAL_HEADER.SizeOfImage
    va = base + rva
    imports = {s.name: s.address for entry in pe.DIRECTORY_ENTRY_IMPORT for s in entry.imports if s.name}
    scratch = va + 0x300
    code = bytearray()

    def call(target):
        """发射有符号相对调用，地址完全封装在当前版本适配中。"""
        code.extend(b"\xe8" + struct.pack("<i", target - va - len(code) - 5))

    def mov(address, value):
        """发射绝对地址写常量，只访问本节拥有的 scratch。"""
        code.extend(b"\xc7\x05" + struct.pack("<II", address, value))

    def branch(op):
        """返回待回填跳转位置，生成后统一核对桩尺寸。"""
        code.extend(op)
        at = len(code)
        code.extend(bytes(4))
        return at

    call(0x6d16c0)
    code.extend(bytes.fromhex("9c60833d004f470704"))
    exits = [branch(b"\x0f\x85")]
    code.extend(b"\x83\x3d" + struct.pack("<I", scratch) + b"\0")
    exits.append(branch(b"\x0f\x85"))
    code.extend(b"\xff\x05" + struct.pack("<I", scratch + 4))
    code.extend(b"\x81\x3d" + struct.pack("<II", scratch + 4, 30))
    exits.append(branch(b"\x0f\x85"))
    mov(scratch, 1)
    code.extend(b"\x68" + struct.pack("<I", va + 0x100) + b"\x33\xf6")
    call(0x59d070)
    code.extend(bytes.fromhex("83c404"))
    end = len(code)
    code.extend(bytes.fromhex("619dc3"))
    for at in exits:
        struct.pack_into("<i", code, at, end - at - 4)
    if len(code) > 0x100:
        raise ValueError("M2 scheduler overflow")
    code.extend(bytes(0x100 - len(code)))
    code.extend(bytes.fromhex("9c60"))
    mov(scratch, 2)
    code.extend(b"\xa1" + struct.pack("<I", scratch + 16) + b"\x85\xc0")
    cached = branch(b"\x0f\x85")
    code.extend(b"\x68" + struct.pack("<I", va + 0x200) + b"\xff\x15" + struct.pack("<I", imports[b"LoadLibraryA"]))
    code.extend(b"\x85\xc0")
    fails = [branch(b"\x0f\x84")]
    code.extend(b"\x68" + struct.pack("<I", va + 0x240) + b"\x50\xff\x15" + struct.pack("<I", imports[b"GetProcAddress"]))
    code.extend(b"\x85\xc0")
    fails.append(branch(b"\x0f\x84"))
    code.extend(b"\xa3" + struct.pack("<I", scratch + 16))
    struct.pack_into("<i", code, cached, len(code) - cached - 4)
    code.extend(b"\xff\xd0\xa3" + struct.pack("<I", scratch + 8))
    end = len(code)
    for at in fails:
        struct.pack_into("<i", code, at, end - at - 4)
    mov(scratch, 0)
    mov(scratch + 4, 0)
    code.extend(bytes.fromhex("619dc3"))
    if len(code) > 0x200:
        raise ValueError("M2 pump overflow")
    content = bytearray(0x400)
    content[:len(code)] = code
    for at, value in ((0x200, b"room_bridge2697.dll\0"), (0x240, b"M2RoomPump\0")):
        content[at:at + len(value)] = value
    alignment = pe.OPTIONAL_HEADER.FileAlignment
    raw_at = (len(raw) + alignment - 1) // alignment * alignment
    raw_size = (len(content) + alignment - 1) // alignment * alignment
    raw.extend(bytes(raw_at - len(raw)))
    raw.extend(content + bytes(raw_size - len(content)))
    header = pe.sections[-1].get_file_offset() + 40
    if header + 40 > pe.OPTIONAL_HEADER.SizeOfHeaders or raw[header:header + 40] != bytes(40):
        raise ValueError("No space for M2 PE section")
    struct.pack_into("<8sIIIIIIHHI", raw, header, b".m2room", len(content), rva, raw_size, raw_at, 0, 0, 0, 0, 0xe0000020)
    struct.pack_into("<H", raw, pe.FILE_HEADER.get_field_absolute_offset("NumberOfSections"), pe.FILE_HEADER.NumberOfSections + 1)
    struct.pack_into("<I", raw, pe.OPTIONAL_HEADER.get_field_absolute_offset("SizeOfImage"), rva + 0x1000)
    struct.pack_into("<I", raw, pe.OPTIONAL_HEADER.get_field_absolute_offset("SizeOfCode"), pe.OPTIONAL_HEADER.SizeOfCode + raw_size)
    site = 0x6d3ce8
    offset = pe.get_offset_from_rva(site - base)
    if raw[offset:offset + 5] != b"\xe8" + struct.pack("<i", 0x6d16c0 - site - 5):
        raise ValueError("M2 safe queue call mismatch")
    raw[offset:offset + 5] = b"\xe8" + struct.pack("<i", va - site - 5)
    exe.write_bytes(raw)
    args.room_mailbox = RoomMailbox(args.player_channel, stage, args.run_directory)
    return {"dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest(), "scratch": hex(scratch),
            "native_id": args.player_channel.player["native_id"], "contract_version": 1,
            "candidate_sha256": hashlib.sha256(raw).hexdigest()}
