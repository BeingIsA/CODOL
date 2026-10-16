"""开发安全档案的原生线格式与写入边界回归。"""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from codol_control.runtime.development_profile2697 import publish_development_profiles


class DevelopmentProfileTests(unittest.TestCase):
    """验证原生解码器要求的长度/CRC/schema 和成员隔离，不启动游戏。"""

    def test_native_payload_and_no_overwrite(self):
        """文件仅以成员 UID 命名，原生可验证 CRC，重复发布拒绝覆盖。"""
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            players = [{'native_id': 6363899815}]
            publish_development_profiles(folder, players)
            raw = zlib.decompress((folder / 'mpdata_s.6363899815').read_bytes())
            self.assertEqual(len(raw), 32768)
            self.assertEqual(struct.unpack_from('<I', raw)[0], zlib.crc32(raw[4:]))
            self.assertEqual(struct.unpack_from('<II', raw, 4), (19, 0xd84aea8a))
            self.assertEqual(struct.unpack_from('<I', raw, 4 + 4883)[0], 70)
            self.assertFalse((folder / 'mpdata_s').exists())
            with self.assertRaises(FileExistsError):
                publish_development_profiles(folder, players)
