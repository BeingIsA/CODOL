"""运行时用例：契约加载、真实探测器（合成目录）与 API 呈现。

深度探测不在测试里对工作区 1.1GB 资产做哈希；真实探测器用
按契约结构搭建的临时目录验证判定逻辑（通过/损坏/缺失各路径）。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from support import ServiceInstance, free_port


def _sha(path: Path) -> str:
    """临时目录小文件的 SHA-256。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _build_synthetic_workspace(root: Path, port: int, corrupt_asset: bool = False) -> None:
    """按契约相对路径搭建最小可用“工作区”：

    <root>/gs/… 扮演 GameServer（EXE、审计、资产清单），
    <root>/mirror.bin 扮演版本恢复镜像（路径在契约里相对工作区根）。
    """
    gs = root / "gs"
    (gs / "native/bin2697").mkdir(parents=True)
    (gs / "manifests").mkdir(parents=True)
    (gs / "assets/dummy").mkdir(parents=True)
    exe = gs / "native/bin2697/codol_dedrun.exe"
    exe.write_bytes(b"fake-exe")
    mirror = root / "mirror.bin"
    mirror.write_bytes(b"fake-mirror")
    asset = gs / "assets/dummy/a.bin"
    asset.write_bytes(b"asset-a")

    audit = {"passed": True, "sha256": _sha(exe)}
    (gs / "manifests/network-audit.json").write_text(json.dumps(audit), encoding="utf-8")
    # 清单条目路径相对 GameServer 根（与真实 asset-import.json 的 "assets/…" 口径一致）。
    entries = [{"path": "assets/dummy/a.bin", "sha256": _sha(asset)}]
    (gs / "manifests/asset-import.json").write_text(json.dumps(entries), encoding="utf-8")

    contract = {
        "version": 1,
        "artifact": {"exe": "gs/native/bin2697/codol_dedrun.exe",
                     "audit_manifest": "gs/manifests/network-audit.json"},
        "precheck": {"mirror": {"path": "mirror.bin", "sha256": _sha(mirror)},
                     "assets_manifest": "gs/manifests/asset-import.json",
                     "assets_root": "gs/assets"},
        "launch": {"supported_content_v1": [{"map": "mp_fake", "gametype": "war"}]},
        "launch_cvars": {"net_port": {"default": port}},
    }
    (gs / "contracts/native-runtime-v1.json").parent.mkdir(parents=True, exist_ok=True)
    (gs / "contracts/native-runtime-v1.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8")

    if corrupt_asset:
        asset.write_bytes(b"tampered")  # 写在清单生成之后：哈希不再匹配


class ContractLoadTests(unittest.TestCase):
    """契约加载与错误处理。"""

    def test_missing_contract_raises(self):
        from codol_control.runtime.contract import ContractError, load_contract

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContractError):
                load_contract(Path(tmp))

    def test_load_real_contract(self):
        from codol_control.runtime.contract import load_contract

        gameserver = Path(__file__).resolve().parents[2]  # …/GameServer
        contract = load_contract(gameserver)
        self.assertEqual(contract.version, 1)
        self.assertEqual(contract.control_port, 28969)
        self.assertEqual(contract.mirror_sha256,
                         "24b4ef2502d3661df178f557192534098c714353cf8c2a00a2ffb26af2bb97e4")


class RuntimeProberTests(unittest.TestCase):
    """真实探测器对合成工作区的判定。"""

    def _probe(self, corrupt_asset: bool):
        from codol_control.runtime.contract import load_contract
        from codol_control.runtime.probe import RuntimeProber

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_synthetic_workspace(root, free_port(), corrupt_asset)
            prober = RuntimeProber(load_contract(root / "gs"))
            return prober.deep_probe()

    def test_all_checks_pass(self):
        """结构完好时 ready=True 且无问题描述。"""
        result = self._probe(corrupt_asset=False)
        self.assertTrue(result.ready)
        self.assertEqual(result.problems, [])
        self.assertEqual(result.assets_total, 1)
        self.assertEqual(result.supported_content[0]["map"], "mp_fake")

    def test_corrupted_asset_fails_ready(self):
        """单个资产被改动即不通过，并在失败明细中给出路径。"""
        result = self._probe(corrupt_asset=True)
        self.assertFalse(result.ready)
        self.assertIn("资产校验未全部通过", " ".join(result.problems))
        self.assertTrue(any("a.bin" in f for f in result.asset_failures))


class RuntimeApiTests(unittest.TestCase):
    """运行时 API 的呈现（假探测器显著标识）。"""

    @classmethod
    def setUpClass(cls):
        cls.service = ServiceInstance()
        status, body = cls.service.login(cls.service.initial_password())
        assert status == 200, body
        cls.token = body["token"]

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def test_runtime_endpoint_marks_fake(self):
        """假探测器结果必须带 fake=True 且 ready=False。"""
        status, body = self.service.request("GET", "/api/runtime", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["probe"]["fake"])
        self.assertFalse(body["probe"]["ready"])
        self.assertIn("假探测器", " ".join(body["probe"]["problems"]))

    def test_explicit_probe_updates_cache(self):
        """POST /runtime/probe 强制重探并返回同结构结果。"""
        status, body = self.service.request("POST", "/api/runtime/probe", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["probe"]["fake"])
        self.assertTrue(body["probe"]["checked_at"])


if __name__ == "__main__":
    unittest.main()
