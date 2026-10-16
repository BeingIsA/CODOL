"""资源层：把安装的全部 IFS 包解成一份完整资源基线。

## 为什么需要它

运行时的"缺文件再补件"链路实测是坏的：一次会话里 174 次缺资源、0 次补上。
原因有两层——索引只扫每个包的前 1MB 清单（包里排在后面的资源根本没被收录，
连补件流程都不会触发），而且补件要在游戏等着的时候现解整个包。

本模块换一条路：**启动之前**把安装目录下的 IFS 包全部解开并解码，铺成一个
完整的资源目录（基线）。staging 时用硬链接把基线铺进副本，运行时不再需要
碰运气补件，缺资源的概率从"碰运气"降到"构建时就能发现"。

## 设计要点

- **包级断点续做**：每个包的状态写进 ``report.json``，中断后重跑只处理未完成的
  包。全量构建耗时以数十分钟计，必须能续。
- **同名资源先到先得**：不同包可能带同名文件。按包处理顺序先写者胜，后来者
  记进 ``shadowed_resources`` 并附双方摘要，绝不静默覆盖。
- **文件名大小写必须还原**：包内资源的 AES-CTR 初始化向量由**打包时的原始
  文件名**参与构造（见 ``codec.decode``），而提取出来的目录项可能被改成小写。
  因此从提取产物里的 ``.lst`` 清单还原原始大小写，再据此解码。
- **落盘原子化**：先写 ``<目标>.decode-pending`` 再 ``replace``，绝不留下半截
  文件，也绝不覆盖已有内容（已存在且摘要一致就跳过）。
- **只读原始安装目录**：直接从原始包路径提取，不复制包副本，省一份 6GB。

产物布局（``destination`` 默认为 ``assets/resource-baseline``）：

```
resource-baseline/
  report.json          包级状态、统计、失败项与同名冲突
  manifest.jsonl       每个落盘文件一行：路径、字节、摘要、来源包
  decoded/             <资源原路径>  直接可给客户端用的字节
  _extracted/<包名>/   <包名> 提取产物（用于增量，可在完成后删除）
"""
import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..paths import ASSETS
from .codec import decode

# 提取工具与它依赖的官方解包 DLL，都随项目自带。
EXTRACTOR = ASSETS / 'extract_local_ifs.exe'
# 提取工具认可的返回码：0 成功，10 为"部分条目跳过"仍算成功。
EXTRACT_EXIT_CODES = (0, 10)
# 提取产物里不是游戏资源的辅助文件。
AUXILIARY_SUFFIXES = ('.lst', '.tsv')
# fastfile 本身是明文（IWff 头），不需要解码。
FASTFILE_MAGIC = b'IWff'


def package_archives(game):
    """列出安装目录下全部 IFS 包，按包名排序。

    排序是刻意固定的：同名资源按包处理顺序先到先得，固定顺序才能让构建
    结果可复现。``ff_``（fastfile）在字典序上先于 ``lf_``，正好让核心
    fastfile 先占位。
    """
    folder = Path(game) / 'IIPS' / 'IIPSDownload'
    return sorted(folder.glob('*.ifs'), key=lambda item: item.name)


def casing_map(extracted):
    """读提取产物里的 .lst 清单，还原每个资源的原始文件名。

    返回 {小写相对路径: 打包时的文件名}。解码需要这个名字，因为 AES-CTR
    的 IV 由文件名的 CRC32 参与构造，大小写错了就解不出来。
    """
    mapping = {}
    for listing in extracted.glob('*.lst'):
        try:
            text = listing.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            clean = line.replace('\\', '/').strip()
            if clean:
                mapping[clean.lower()] = clean.rsplit('/', 1)[-1]
    return mapping


def extract_archive(game, archive, destination):
    """把一个 IFS 包提取到 ``destination``，返回 (提取目录, 返回码, 摘要)。

    先记录输入包摘要，提取后再核对一次，确保处理过程中包没被改动。
    """
    folder = destination / archive.stem
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    task = subprocess.run(
        [str(EXTRACTOR), str(Path(game) / 'IIPS' / 'IFS2.dll'), str(archive), str(folder)],
        capture_output=True, text=True)
    after = hashlib.sha256(archive.read_bytes()).hexdigest()
    if before != after:
        raise RuntimeError('IFS 包在提取期间被改动：' + archive.name)
    if task.returncode not in EXTRACT_EXIT_CODES:
        raise RuntimeError('提取失败（退出码 %d）：%s' % (task.returncode, task.stderr.strip()))
    return folder, task.returncode, before


def _decode_one(job):
    """解码单个文件（在工作线程里执行）；失败返回错误信息而不抛出。

    cryptography 与 zlib 都会释放 GIL，因此用线程池并行解码是有效的。
    """
    source, relative, decode_name = job
    raw = source.read_bytes()
    data = raw if raw.startswith(FASTFILE_MAGIC) else decode(raw, decode_name)
    return relative, data


def build(game, destination, archives=None, jobs=4, progress=None):
    """构建/续做资源基线，返回 report 字典。

    参数：
    - ``game``        原始客户端安装目录（只读）；
    - ``destination`` 基线目录（不存在则创建）；
    - ``archives``    指定要处理的包，None 表示全部；
    - ``jobs``        包内并行解码的线程数；
    - ``progress``    可选的回调 ``(包名, 序号, 总数)``，用于打印进度。

    已完成的包直接从 report.json 读取状态跳过，因此中断后重跑是安全的。
    """
    destination = Path(destination)
    decoded = destination / 'decoded'
    extracted_root = destination / '_extracted'
    decoded.mkdir(parents=True, exist_ok=True)
    extracted_root.mkdir(parents=True, exist_ok=True)
    report_path = destination / 'report.json'
    manifest_path = destination / 'manifest.jsonl'
    # 续做：读回上一次的包状态与文件清单，避免重复劳动。
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding='utf-8'))
    else:
        report = {'game': str(game), 'destination': str(destination), 'files': 0,
                  'failed': [], 'shadowed_resources': [], 'archives': []}
    done = {item['name']: item for item in report['archives'] if item.get('status') == 'ok'}
    owners = {}
    if manifest_path.is_file():
        with manifest_path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    owners.setdefault(row['path'].lower(), row)
    selected = list(archives) if archives else package_archives(game)
    manifest = manifest_path.open('a', encoding='utf-8')
    try:
        for index, archive in enumerate(selected, 1):
            if archive.name in done:
                continue
            if progress:
                progress(archive.name, index, len(selected))
            entry = {'name': archive.name, 'status': 'failed'}
            try:
                folder, exit_code, digest = extract_archive(game, archive, extracted_root)
                casing = casing_map(folder)
                entry.update(sha256=digest, extract_exit=exit_code)
                jobs_list = []
                for path in sorted(folder.rglob('*')):
                    if not path.is_file() or path.suffix.lower() in AUXILIARY_SUFFIXES:
                        continue
                    relative = path.relative_to(folder)
                    key = relative.as_posix().lower()
                    jobs_list.append((path, relative, casing.get(key, path.name)))
                written = 0
                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    # executor.map 按输入顺序产出，落盘顺序确定、内存占用有界。
                    for relative, data in pool.map(_decode_one, jobs_list):
                        key = relative.as_posix().lower()
                        target = decoded / relative
                        digest = hashlib.sha256(data).hexdigest()
                        existing = owners.get(key)
                        if existing is not None:
                            # 同名资源：先到先得，后来者只记录不覆盖。
                            if existing['sha256'] != digest:
                                report['shadowed_resources'].append({
                                    'path': str(relative), 'kept_archive': existing['archive'],
                                    'kept_sha256': existing['sha256'],
                                    'skipped_archive': archive.name, 'skipped_sha256': digest})
                            continue
                        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                            pass
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            pending = target.with_name(target.name + '.decode-pending')
                            pending.write_bytes(data)
                            pending.replace(target)
                        row = {'path': str(relative), 'bytes': len(data), 'sha256': digest,
                               'archive': archive.name}
                        owners[key] = row
                        report['files'] += 1
                        written += 1
                        manifest.write(json.dumps(row) + '\n')
                manifest.flush()
                entry.update(status='ok', decoded=written)
            except Exception as exc:
                entry['error'] = repr(exc)
            report['archives'].append(entry)
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        manifest.close()
    report['ok_archives'] = sum(1 for item in report['archives'] if item.get('status') == 'ok')
    report['failed_archives'] = [item['name'] for item in report['archives'] if item.get('status') != 'ok']
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def discard_extracted(destination):
    """删除提取中间产物（省约 6GB），基线解码结果不受影响。"""
    target = Path(destination) / '_extracted'
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False
