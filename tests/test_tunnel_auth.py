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

    def test_generate_token_is_32_chars(self):
        mgr = TokenManager()
        token = mgr.generate_token()
        assert len(token) == 32
        assert token.isalnum()

    def test_validate_generated_token(self):
        mgr = TokenManager()
        token = mgr.generate_token()
        assert mgr.validate(token) is True

    def test_validate_unknown_token(self):
        mgr = TokenManager()
        assert mgr.validate("unknown_token") is False

    def test_validate_before_generate(self):
        mgr = TokenManager()
        assert mgr.validate("any") is False

    def test_new_token_invalidates_old(self):
        mgr = TokenManager()
        token1 = mgr.generate_token()
        assert mgr.validate(token1) is True
        token2 = mgr.generate_token()
        assert mgr.validate(token1) is False
        assert mgr.validate(token2) is True

    def test_generate_token_unique(self):
        mgr = TokenManager()
        tokens = {mgr.generate_token() for _ in range(100)}
        assert len(tokens) == 100
