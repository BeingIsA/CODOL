# Version 2.6.9.7 research port; see docs/provenance.json.
"""版本适配层：资源文件门与加载计时（2.6.9.7 地址与机器码）。

三部分能力，全部服务"客户端副本读不到资源时由宿主补件"这条链路：

1. ``apply_video_patch`` —— 副本内的视频设置补丁（用本地的延迟
   vid_restart 替代在线 GPU 上传路径）。
2. ``resource_name_hash`` / ``build_resource_filter`` —— 把 Provider 索引中
   的全部资源名做 FNV 哈希，填成一张 100 万位的位图（两个哈希位同时
   命中才算候选），供注入到客户端的文件门做超轻量预筛。
3. ``build_file_gate`` —— 生成一段 32 位机器码桩，替换客户端 IAT 中的
   CreateFileA/W：打开失败且只读且文件不存在时，先过位图预筛，命中则
   落入陷阱指令暂停到调试宿主；宿主补件后写标记让桩重试一次原调用。
   正常路径（文件存在）完全直通，开销近乎为零。
"""
import struct


def apply_video_patch(raw, pe):
    """在副本镜像里打视频设置补丁，返回补丁记录。

    把硬编码的命令字符串地址从在线 'vid_restart' 换成本地副本内的
    另一处字符串，使画质应用走本地延迟重启，不依赖在线 GPU 上传。
    """
    offset = pe.get_offset_from_rva(0x6c5730 - 0x400000)
    before = bytes.fromhex('b828ddcc0033c9e8a478edffc20400')
    command = pe.get_offset_from_rva(0xcc7b48 - 0x400000)
    assert raw[command:command + 13] == b'vid_restart\n\x00'
    after = bytes.fromhex('b8487bcc0033c9e8a478edffc20400')
    assert raw[offset:offset + len(before)] == before
    raw[offset:offset + len(after)] = after
    return dict(site='0x6c5730', before=before.hex(), after=after.hex(), purpose='Apply locally through deferred vid_restart without online GPU upload')


# 位图位数：2^20 = 100 万个槽位，两个哈希位同时命中才认定为候选资源。
RESOURCE_FILTER_BITS = 1 << 20


def resource_name_hash(name):
    """资源名的 FNV-1a 哈希：路径分隔符归一为 /，ASCII 字母转小写。"""
    value = 0x811c9dc5
    for char in name.replace('\\', '/'):
        number = ord(char)
        if 65 <= number <= 90:
            number |= 32
        value = (value ^ number) * 0x1000193 & 0xffffffff
    return value


def build_resource_filter(provider):
    """由 Provider 索引构建位图过滤器；provider 类型不符时返回 None。

    索引名为空或含非 ASCII/NUL 时同样放弃（位图内联在机器码里重算哈希，
    字符集必须与 32 位代码的处理能力一致）。
    """
    from ..resources.provider import Provider
    if type(provider) is not Provider:
        return None
    names = list(provider.index)
    if not all((isinstance(name, str) and name.isascii() and ('\x00' not in name) for name in names)):
        return None
    bitmap = bytearray(RESOURCE_FILTER_BITS // 8)
    for name in names:
        value = resource_name_hash(name)
        # 双哈希槽位：低 20 位与右移 12 位各占一位，降低误报率。
        for bit in (value & RESOURCE_FILTER_BITS - 1, value >> 12):
            bitmap[bit >> 3] |= 1 << (bit & 7)
    return bytes(bitmap)


def build_file_gate(base, original, get_error, set_error, counter, bitmap=None, wide=False):
    """生成 CreateFileA/W 的门桩机器码，返回 (机器码, 陷阱地址)。

    参数：
    - ``base``       桩在客户端进程内的加载地址（相对调用以此计算）；
    - ``original``   真正的 CreateFileA/W 地址；
    - ``get_error``/``set_error`` —— GetLastError/SetLastError 地址；
    - ``counter``    打开次数计数器地址；
    - ``bitmap``     资源位图地址（None 则不做名称预筛）；
    - ``wide``       True 处理 W 版（UTF-16 文件名），False 处理 A 版。

    桩逻辑（伪代码）：
        handle = original(...)           # 先照常打开
        err = GetLastError()
        if handle != -1: goto finish     # 成功：直接返回
        if 参数不是 GENERIC_READ: goto finish
        if 参数不是 OPEN_EXISTING: goto finish
        if err not in (2, 3): goto finish  # 只处理"文件/路径不存在"
        [可选] 位图双哈希未命中: goto finish  # 该文件不可能是可补资源
        retry = 0
        trap:                            # 陷阱指令（int3，宿主接管）
            if retry == 0: goto finish
            handle = original(...)       # 宿主补件后重试一次
            err = GetLastError()
        finish:
            SetLastError(err)
            return handle
    宿主在陷阱处补件后，把桩栈帧内 [ebp-0xc]（retry 标记）写 1 触发重试。
    """
    code = bytearray.fromhex('558bec83ec10')
    branches = []

    def call(address):
        code.extend(b'\xe8' + struct.pack('<I', address - base - len(code) - 5 & 0xffffffff))

    def jump(opcode, label):
        # 记录跳转位置，最后统一回填相对偏移。
        code.extend(bytes.fromhex(opcode))
        branches.append((len(code), label))
        code.extend(bytes(4))

    def open_file():
        # 按原样转发栈上的 7 个参数并调用真正的 CreateFileA/W。
        for offset in range(32, 7, -4):
            code.extend(bytes([255, 117, offset]))
        call(original)
        code.extend(bytes.fromhex('8945fc'))  # 保存句柄到 [ebp-4]
        call(get_error)
        code.extend(bytes.fromhex('8945f8'))  # 保存错误码到 [ebp-8]

    code.extend(b'\xf0\xff\x05' + struct.pack('<I', counter))  # lock inc [counter]
    open_file()
    code.extend(bytes.fromhex('837dfcff'))        # 句柄 == -1（失败）？
    jump('0f85', 'finish')
    code.extend(bytes.fromhex('837d1803'))        # 创建方式 == OPEN_EXISTING？
    jump('0f85', 'finish')
    code.extend(bytes.fromhex('817d0c00000080'))  # 访问模式 == GENERIC_READ？
    jump('0f85', 'finish')
    code.extend(bytes.fromhex('83f802'))          # 错误码 == 2（文件不存在）？
    jump('0f84', 'missing')
    code.extend(bytes.fromhex('83f803'))          # 错误码 == 3（路径不存在）？
    jump('0f85', 'finish')
    labels = {'missing': len(code)}
    if bitmap is not None:
        # ---- 名称预筛：在桩内重算 FNV 哈希并查位图 ----
        code.extend(bytes.fromhex('8b4d08'))      # ecx = 文件名指针
        labels['name_reset'] = len(code)
        code.extend(bytes.fromhex('c745f000000000'))  # [ebp-0x10] = 0（起始分隔段）
        code.extend(b'\xb8' + struct.pack('<I', 0x811c9dc5))  # eax = FNV 偏移基
        labels['name_loop'] = len(code)
        # 读一个字符（宽字符 0fb711 / 窄字符 0fb611），NUL 即结束。
        code.extend(bytes.fromhex('0fb711' if wide else '0fb611'))
        code.extend(bytes.fromhex('85d2'))
        jump('0f84', 'name_done')
        code.extend(bytes([131, 193, 2 if wide else 1]))  # 指针前移
        for separator in (47, 92):  # '/' 与 '\\'：进入新分隔段
            code.extend(bytes([131, 250, separator]))
            jump('0f84', 'name_separator')
        code.extend(bytes.fromhex('83fa2e'))  # '.'：检查扩展名段
        jump('0f85', 'name_lower')
        for char in (46, 47, 92):  # 扩展名段内再遇 './\\'：非法，直接放行失败
            code.extend((b'f\x839' if wide else b'\x809') + bytes([char]))
            jump('0f84', 'resource_trap')
        labels['name_lower'] = len(code)
        code.extend(bytes.fromhex('83fa41'))  # 'A'..'Z' 转小写
        jump('0f82', 'name_hash')
        code.extend(bytes.fromhex('83fa5a'))
        jump('0f87', 'name_hash')
        code.extend(bytes.fromhex('83ca20'))  # |= 0x20
        labels['name_hash'] = len(code)
        code.extend(bytes.fromhex('33c269c093010001'))  # 哈希迭代
        jump('e9', 'name_loop')
        labels['name_separator'] = len(code)
        # 分隔符归一：'\\' 与 '/' 等价，段计数 +1 后继续哈希。
        for char in (47, 92):
            code.extend((b'f\x839' if wide else b'\x809') + bytes([char]))
            jump('0f84', 'resource_trap')
        code.extend(bytes.fromhex('837df000'))  # [ebp-0x10] == 0？
        jump('0f85', 'name_separator_hash')
        code.extend(bytes.fromhex('894df0'))    # 段数存入 [ebp-0x10]
        labels['name_separator_hash'] = len(code)
        code.extend(bytes.fromhex('ba2f000000'))  # edx = '/'
        jump('e9', 'name_hash')
        labels['name_done'] = len(code)
        # 双哈希槽位检查：BT 指令测位，任一为 0 即不是候选资源。
        code.extend(bytes.fromhex('8bd0'))
        code.extend(b'%' + struct.pack('<I', RESOURCE_FILTER_BITS - 1))
        code.extend(b'\x0f\xa3\x05' + struct.pack('<I', bitmap))
        jump('0f83', 'name_next_suffix')
        code.extend(bytes.fromhex('c1ea0c'))
        code.extend(b'\x0f\xa3\x15' + struct.pack('<I', bitmap))
        jump('0f82', 'resource_trap')
        labels['name_next_suffix'] = len(code)
        # 紧跟分隔符的 '.' 开头段（如 "\."）不处理：重置回循环起点。
        code.extend(bytes.fromhex('8b4df085c9'))
        jump('0f84', 'finish')
        jump('e9', 'name_reset')
    labels['resource_trap'] = len(code)
    code.extend(bytes.fromhex('c745f400000000'))  # retry = 0（[ebp-0xc]）
    trap = len(code)
    code.append(144)  # NOP 占位：宿主会把这里改写成 0xCC 断点指令
    code.extend(bytes.fromhex('837df400'))  # retry != 0 才重试
    jump('0f84', 'finish')
    open_file()  # 补件后的重试调用
    labels['finish'] = len(code)
    code.extend(bytes.fromhex('ff75f8'))
    call(set_error)  # 恢复 GetLastError 语义
    code.extend(bytes.fromhex('8b45fc8be55dc21c00'))  # 返回句柄并平栈
    for at, label in branches:
        struct.pack_into('<i', code, at, labels[label] - at - 4)
    assert len(code) < 512
    return (bytes(code), base + trap)


def read_cstring(mem, address, wide=False, limit=2048, encoding='utf-8'):
    """安全读取客户端内存中的字符串：按页小块读取，绝不越过不可读页。

    ``mem`` 为读内存函数；``wide`` 为 True 时按 UTF-16LE 解析。
    遇到 NUL 终止；页面不可读时止步，返回已读部分的替换文本。
    """
    step = 2 if wide else 1
    result = bytearray()
    while len(result) < limit * step:
        cursor = address + len(result)
        # 单次最多读到本页末尾，且不超过上限。
        size = min(256, 4096 - (cursor & 4095), limit * step - len(result))
        size -= size % step
        if not size:
            break
        try:
            block = mem(cursor, size)
        except OSError:
            try:
                block = mem(cursor, step)
            except OSError:
                break
        if not block:
            break
        for i in range(0, len(block) - step + 1, step):
            piece = block[i:i + step]
            if piece == bytes(step):
                return result.decode('utf-16le' if wide else encoding, errors='replace')
            result.extend(piece)
    return result.decode('utf-16le' if wide else encoding, errors='replace')


class LoadingClock:
    """加载计时：把"宿主处理调试事件"与"客户端自然耗时"分开度量。

    调试事件处理期间客户端是被暂停的；本类把宿主处理耗时单独累计，
    不将其计入引擎 CPU。事件等待、OS 调度与观察者工作仍计入自然时间，
    因此报告注明口径，不冒充引擎耗时。
    """

    def __init__(self, clock=None):
        import time
        self.clock = clock or time.perf_counter
        self.origin = None      # 客户端恢复运行的时间起点
        self.active = None      # 当前正在处理的事件起始时间
        self.event_label = None
        self.handlers = {}      # 标签 -> {calls, seconds, max_seconds}
        self.milestones = []    # 里程碑时刻的计时快照
        self.extensions = {}    # 缺失文件计数（按扩展名；当前不区分）

    def resume(self):
        """客户端恢复运行：记录自然时间起点。"""
        self.origin = self.clock()

    def begin(self, event_code):
        """开始处理一个调试事件。"""
        assert self.active is None
        self.active = self.clock()
        self.event_label = 'debug-event-' + str(event_code)

    def label(self, label):
        """为当前事件改用更有意义的标签（如命中的断点名）。"""
        self.event_label = label

    def end(self):
        """结束当前事件处理，累计耗时。"""
        if self.active is None:
            return
        elapsed = self.clock() - self.active
        row = self.handlers.setdefault(self.event_label, {'calls': 0, 'seconds': 0.0, 'max_seconds': 0.0})
        row['calls'] += 1
        row['seconds'] += elapsed
        row['max_seconds'] = max(row['max_seconds'], elapsed)
        self.active = None

    def mark(self, name, resource_gate):
        """在里程碑处记录：自恢复以来的自然时间与宿主累计处理时间。"""
        row = dict(name=name, since_resume_seconds=None if self.origin is None else self.clock() - self.origin, completed_handler_seconds=sum((x['seconds'] for x in self.handlers.values())), missing_calls=resource_gate.get('missing_calls', 0), provider_seconds=resource_gate.get('provider_seconds', 0.0))
        if len(self.milestones) < 256:
            self.milestones.append(row)
        return row

    def missing(self, filename):
        """登记一次缺失文件（当前按单一桶计数，不做扩展名细分）。"""
        ext = True
        if ext not in self.extensions and len(self.extensions) >= 64:
            ext = '(other)'
        self.extensions[ext] = self.extensions.get(ext, 0) + 1

    def report(self):
        """输出计时报告（含口径说明）。"""
        return dict(milestones=self.milestones, handlers=self.handlers, missing_extensions=self.extensions, note='Handler seconds exclude debug-event wait, OS scheduling and observer work outside event handling.')
