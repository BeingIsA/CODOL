# Version 2.6.9.7 research port; see docs/provenance.json.
"""资源供给层：扫描 IFS 包建立索引，在客户端缺文件时提取、解码并补件。

Provider 是一个可调用对象，由版本适配层的"资源文件门"（v2697.resource_gate）
在客户端读文件失败时调用。设计原则：正常文件读取完全走原系统路径，
只有缺失时才通知本提供器——不把游戏的每一次打开文件都变成昂贵处理。

流程（每次缺文件只尝试一次，避免死循环）：
1. 按四类模式过滤（贴图 iwi / 模型 / 音效 / 中文 fastfile）；
2. 查 index.json 找到候选 .ifs 包；
3. extract_local_ifs.exe 提取（包先复制到缓存目录核对哈希）；
4. codec.decode 解码（fastfile 本身已是 IWff 头的跳过解码）；
5. 以排他方式写进副本目录 + 写入解码缓存供下次直接复用。

注意：本模块在导入期读取 ``CODOL2697_GAME_DIR`` 环境变量定位原始
客户端，必须由 app.context.prepare 先设置再导入。
"""
from pathlib import Path
import hashlib, json, re, shutil, subprocess
from .codec import decode
from ..paths import ASSETS as ROOT
GAME = Path(__import__('os').environ['CODOL2697_GAME_DIR'])


class Provider:
    """IFS 资源索引与按需补件器。

    构造时扫描原始客户端 ``IIPS/IIPSDownload/*.ifs`` 每个包的前 1MB，
    收集四类资源名建立索引并重建 local-resource-cache/index.json。
    ``stage`` 属性由 staging 流程回填为本次副本目录，之后 ``__call__``
    才会实际补件（staging 会把缓存里已有的资源预先放进副本）。
    """

    def __init__(self):
        self.index = {}        # 资源名(小写) -> [(ifs 包路径, 包内名), ...]
        self.stage = None      # 本次客户端副本目录（staging.build 回填）
        self.attempted = set() # 已尝试过的相对路径，保证每路径只补一次
        self.extracted = {}    # 已提取过的包名 -> 包哈希（避免重复提取）
        self.events = []       # 补件事件流水（写入 local-resource-supplies.json）
        self.cache = ROOT / 'local-resource-cache'
        self.cache.mkdir(exist_ok=True)
        # 只索引四类可补资源：贴图、模型目录、音效、中文 fastfile。
        # 文件名部分排除控制字符与 Windows 非法字符。
        pattern = re.compile(b'(?im)^(main[/\\\\][^\\x00-\\x1f\\x7f-\\xff:<>"|?*]+?\\.(?:iwi|mp3|wav)|main[/\\\\]models[/\\\\][a-z0-9_/\\\\-]+|client_shipretail[/\\\\][^\\x00-\\x1f\\x7f-\\xff:<>"|?*]+?\\.ff)\\r?$')
        for archive in (GAME / 'IIPS/IIPSDownload').glob('*.ifs'):
            with archive.open('rb') as stream:
                prefix = stream.read(1024 * 1024)
            for match in pattern.finditer(prefix):
                name = match.group().decode('ascii').strip().replace('\\', '/')
                # 防路径逃逸：拒绝绝对路径与 .. 。
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    continue
                self.index.setdefault(name.lower(), []).append((archive, name))
        (self.cache / 'index.json').write_text(json.dumps({k: [a.name for a, n in v] for k, v in self.index.items()}, indent=2))

    def __call__(self, filename):
        """尝试为客户端补一个缺失文件；不适用则直接返回。

        只处理：副本目录内的相对路径（或指向副本内的绝对路径）、
        尚未尝试过、且命中四类补件模式的读取请求。
        """
        if self.stage is None:
            return
        # 设备路径（\\、\??\）与副本外的绝对路径不是游戏资源请求。
        if not filename or filename.startswith(('\\\\', '\\??\\')):
            return
        target = Path(filename)
        if target.is_absolute() and (not str(target).lower().startswith(str(self.stage).lower() + '\\')):
            return
        if not target.is_absolute():
            target = self.stage / target
        try:
            target = target.resolve()
        except OSError:
            return
        try:
            relative = target.relative_to(self.stage.resolve()).as_posix().lower()
        except ValueError:
            return
        if target.exists() or relative in self.attempted:
            return
        if not (relative.startswith('main/images/') and relative.endswith('.iwi') or (relative.startswith('main/models/') and (not target.suffix)) or (relative.startswith('main/sound/') and target.suffix.lower() in ('.mp3', '.wav')) or (relative.startswith('client_shipretail/chinese/') and target.suffix.lower() == '.ff')):
            return
        self.attempted.add(relative)
        event = {'resource': relative, 'supplied': False, 'attempts': []}
        for archive, name in self.index.get(relative, []):
            folder = self.cache / 'extracted' / archive.stem
            try:
                if archive.name not in self.extracted:
                    # 提取前先把包复制到缓存 inputs/ 并核对哈希，
                    # 保证提取工具处理的是与索引时一致的输入。
                    alias = self.cache / 'inputs' / archive.name
                    alias.parent.mkdir(exist_ok=True)
                    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                    if not alias.exists():
                        shutil.copyfile(archive, alias)
                    assert hashlib.sha256(alias.read_bytes()).hexdigest() == digest
                    task = subprocess.run([str(ROOT / 'extract_local_ifs.exe'), str(GAME / 'IIPS/IFS2.dll'), str(alias), str(folder)], capture_output=True, text=True, timeout=45)
                    assert task.returncode in (0, 10), task.stderr
                    self.extracted[archive.name] = digest
                source = folder / name
                assert source.resolve().is_relative_to(folder.resolve())
                if not source.is_file():
                    continue
                # .lst 清单内可能记录了与包内不同的显示名，用于解码命名。
                decode_name = Path(name).name
                for listing in folder.glob('*.lst'):
                    for item in listing.read_text(encoding='utf-8-sig').splitlines():
                        clean = item.replace('\\', '/')
                        if clean.lower() == relative:
                            decode_name = clean.rsplit('/', 1)[-1]
                            break
                raw = source.read_bytes()
                # fastfile 已是明文（IWff 头）则直接使用，其余走 AES-CTR+zlib。
                data = raw if raw.startswith(b'IWff') else decode(raw, decode_name)
                if target.suffix.lower() == '.iwi':
                    assert data.startswith(b'IWi\t')
                target.parent.mkdir(parents=True, exist_ok=True)
                # 'xb' 排他写入：绝不覆盖已存在的文件（游戏会重试读取）。
                with target.open('xb') as stream:
                    stream.write(data)
                # 同步写入解码缓存；已存在则校验一致，不一致立即暴露问题。
                cached = self.cache / 'decoded' / relative
                cached.parent.mkdir(parents=True, exist_ok=True)
                if cached.exists():
                    assert cached.read_bytes() == data
                else:
                    cached.write_bytes(data)
                event.update(supplied=True, archive=archive.name, archive_sha256=self.extracted[archive.name], bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                break
            except Exception as exc:
                event['attempts'].append({'archive': archive.name, 'error': repr(exc)})
        self.events.append(event)
        (self.stage.parent / 'local-resource-supplies.json').write_text(json.dumps(self.events, indent=2))
