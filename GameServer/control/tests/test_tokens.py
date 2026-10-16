"""令牌与口令散列单元用例：签名、过期、篡改与口令策略。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from codol_control.domain.errors import UnauthenticatedError
from codol_control.infrastructure.tokens import (TokenSigner, generate_secret_hex,
                                                 hash_password, new_salt)


class TokenTests(unittest.TestCase):
    """TokenSigner 的签名/校验/时效。"""

    def setUp(self):
        self.signer = TokenSigner(generate_secret_hex(32))

    def test_sign_and_verify_roundtrip(self):
        """正常签发在有效期内可校验，载荷字段完整。"""
        token = self.signer.sign({"sub": "admin", "ver": 1}, timedelta(minutes=5))
        payload = self.signer.verify(token)
        self.assertEqual(payload["sub"], "admin")
        self.assertEqual(payload["ver"], 1)

    def test_expired_token_rejected(self):
        """过期令牌必须被拒绝。"""
        token = self.signer.sign({"sub": "admin"}, timedelta(minutes=-1))
        with self.assertRaises(UnauthenticatedError):
            self.signer.verify(token)

    def test_tampered_token_rejected(self):
        """篡改载荷或签名必须被拒绝。"""
        token = self.signer.sign({"sub": "admin"}, timedelta(minutes=5))
        raw, sig = token.split(".", 1)
        tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B") + "." + sig
        with self.assertRaises(UnauthenticatedError):
            self.signer.verify(tampered)

    def test_wrong_secret_rejected(self):
        """另一把密钥签的令牌不能通过本校验器。"""
        other = TokenSigner(generate_secret_hex(32))
        token = other.sign({"sub": "admin"}, timedelta(minutes=5))
        with self.assertRaises(UnauthenticatedError):
            self.signer.verify(token)


class PasswordHashTests(unittest.TestCase):
    """PBKDF2 口令散列。"""

    def test_hash_deterministic_with_salt(self):
        """同口令同盐结果一致；不同盐结果不同（彩虹表不可复用）。"""
        salt = new_salt()
        self.assertEqual(hash_password("abc123", salt), hash_password("abc123", salt))
        self.assertNotEqual(hash_password("abc123", salt), hash_password("abc123", new_salt()))

    def test_wrong_password_differs(self):
        """错误口令的散列不等于正确口令的散列。"""
        salt = new_salt()
        self.assertNotEqual(hash_password("abc123", salt), hash_password("abc124", salt))


if __name__ == "__main__":
    unittest.main()
