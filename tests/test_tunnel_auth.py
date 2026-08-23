"""
认证管理单元测试。
测试配对码生成/验证和令牌管理。
"""

import time
from unittest.mock import patch

import pytest

from phonemic.tunnel.auth import PairingCodeManager, TokenManager


class TestPairingCode:
    """测试配对码管理器。"""

    def test_generate_returns_4_digit_code(self):
        mgr = PairingCodeManager()
        code = mgr.generate()
        assert len(code) == 4
        assert code.isdigit()

    def test_validate_correct_code(self):
        mgr = PairingCodeManager()
        code = mgr.generate()
        assert mgr.validate(code) is True

    def test_validate_wrong_code(self):
        mgr = PairingCodeManager()
        mgr.generate()
        assert mgr.validate("9999" if mgr.current() != "9999" else "0000") is False

    def test_validate_without_generate(self):
        mgr = PairingCodeManager()
        assert mgr.validate("1234") is False

    def test_code_expires(self):
        mgr = PairingCodeManager(ttl=0.05)
        code = mgr.generate()
        assert mgr.validate(code) is True
        time.sleep(0.1)
        assert mgr.validate(code) is False

    def test_generate_invalidates_old_code(self):
        mgr = PairingCodeManager()
        code1 = mgr.generate()
        code2 = mgr.generate()
        assert mgr.validate(code1) is False
        assert mgr.validate(code2) is True

    def test_current_returns_none_before_generate(self):
        mgr = PairingCodeManager()
        assert mgr.current() is None
        assert mgr.is_active() is False

    def test_current_returns_code_after_generate(self):
        mgr = PairingCodeManager()
        code = mgr.generate()
        assert mgr.current() == code
        assert mgr.is_active() is True

    def test_current_returns_none_after_expiry(self):
        mgr = PairingCodeManager(ttl=0.05)
        mgr.generate()
        time.sleep(0.1)
        assert mgr.current() is None
        assert mgr.is_active() is False


class TestTokenManager:
    """测试令牌管理器。"""

    def test_generate_token_is_32_chars(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        token = mgr.generate_token()
        assert len(token) == 32
        assert token.isalnum()

    def test_add_and_validate(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        token = mgr.generate_token()
        mgr.add(token, "iPhone")
        assert mgr.validate(token) is True

    def test_validate_unknown_token(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        assert mgr.validate("unknown_token") is False

    def test_revoke(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        token = mgr.generate_token()
        mgr.add(token)
        assert mgr.revoke(token) is True
        assert mgr.validate(token) is False

    def test_revoke_unknown_returns_false(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        assert mgr.revoke("nonexistent") is False

    def test_persistence(self, tmp_path):
        path = tmp_path / "tokens.json"
        mgr1 = TokenManager(storage_path=path)
        token = mgr1.generate_token()
        mgr1.add(token, "iPad")

        mgr2 = TokenManager(storage_path=path)
        assert mgr2.validate(token) is True
        assert mgr2.count() == 1
        assert mgr2.list_all()[token]["name"] == "iPad"

    def test_count(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        assert mgr.count() == 0
        mgr.add("token1")
        mgr.add("token2")
        assert mgr.count() == 2

    def test_generate_token_unique(self, tmp_path):
        mgr = TokenManager(storage_path=tmp_path / "tokens.json")
        tokens = {mgr.generate_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "tokens.json"
        path.write_text("not json")
        mgr = TokenManager(storage_path=path)
        assert mgr.count() == 0
