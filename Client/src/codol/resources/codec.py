# Version 2.6.9.7 research port; see docs/provenance.json.
"""资源供给层：IFS 包内单个资源的解码（AES-CTR + zlib）。

包内字节不能直接当资源使用：按 32KB 分块、以“文件名 CRC32 + 声明大小 +
块偏移 + 块长度”构造 IV 做 AES-CTR 解密，解出的压缩流再按声明大小做
zlib 解压并严格校验长度。密钥为版本固定的 24 字节 AES key。
"""
import struct, zlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 2.6.9.7 IFS 资源的 AES 密钥（研究获得，版本绑定）。
KEY = bytes.fromhex('159a0325e0752e80c6c0942a505c1c688c17ef5399f8683c')


def decode(raw, name):
    """把一个编码资源还原为原始字节。

    ``raw`` 为包内文件内容（末尾 4 字节是原始大小）；``name`` 为包内
    文件名，参与解密 IV 的构造，因此文件名必须与打包时一致。
    任何一步校验失败（大小声明、zlib 完整性、长度一致）都抛
    ``ValueError``，绝不返回半解码数据。
    """
    size = struct.unpack('<I', raw[-4:])[0]
    if not 0 < size <= 512 * 1024 * 1024:
        raise ValueError('Invalid declared size')
    encoded = name.encode('utf-8')
    nonce = zlib.crc32(encoded, len(encoded))
    packed = bytearray()
    # 分块解密：每块的 IV = (文件名nonce, 声明大小, 块偏移, 块长度)。
    for offset in range(0, len(raw) - 4, 32768):
        chunk = raw[offset:min(offset + 32768, len(raw) - 4)]
        iv = struct.pack('<4I', nonce, size, offset, len(chunk))
        cipher = Cipher(algorithms.AES(KEY), modes.CTR(iv)).decryptor()
        packed.extend(cipher.update(chunk) + cipher.finalize())
    # 解压并严格校验：长度必须等于声明大小，且流完整无剩余。
    stream = zlib.decompressobj()
    data = stream.decompress(packed, size + 1)
    if len(data) != size or not stream.eof or stream.unused_data or stream.unconsumed_tail:
        raise ValueError('Length/zlib validation failed')
    return data
