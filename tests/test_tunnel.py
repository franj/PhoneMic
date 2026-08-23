"""
CloudflareTunnel 单元测试。
使用 mock 模拟 subprocess 和文件系统，不依赖真实 cloudflared 二进制。
"""

import os
import subprocess
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from phonemic.tunnel.base import TunnelProvider
from phonemic.tunnel.cloudflare import CloudflareTunnel, _URL_PATTERN


class TestUrlPattern:
    """测试 URL 正则解析。"""

    def test_matches_trycloudflare_url(self):
        line = '2026-08-23T07:25:46Z INF |  https://sectors-istanbul-refinance-princeton.trycloudflare.com'
        match = _URL_PATTERN.search(line)
        assert match is not None
        assert match.group() == 'https://sectors-istanbul-refinance-princeton.trycloudflare.com'

    def test_no_match_plain_text(self):
        assert _URL_PATTERN.search("hello world") is None

    def test_no_match_other_domain(self):
        assert _URL_PATTERN.search("https://example.com") is None

    def test_matches_in_multiline_output(self):
        output = (
            "INF Thank you for trying Cloudflare Tunnel.\n"
            "INF Requesting new quick Tunnel on trycloudflare.com...\n"
            'INF |  https://my-random-url-here.trycloudflare.com  |\n'
            "INF Version 2026.8.2\n"
        )
        for line in output.splitlines():
            match = _URL_PATTERN.search(line)
            if match:
                assert match.group() == 'https://my-random-url-here.trycloudflare.com'
                return
        pytest.fail("URL not found in output")


class TestBinaryDetection:
    """测试 cloudflared 二进制查找逻辑。"""

    def test_explicit_binary_path_exists(self, tmp_path):
        fake_bin = tmp_path / "cloudflared.exe"
        fake_bin.write_text("fake")
        tunnel = CloudflareTunnel(binary_path=str(fake_bin))
        assert tunnel.is_available() is True

    def test_explicit_binary_path_not_found(self):
        tunnel = CloudflareTunnel(binary_path="/nonexistent/cloudflared.exe")
        assert tunnel.is_available() is False

    @patch("shutil.which", return_value=None)
    @patch("phonemic.tunnel.cloudflare.get_bin_dir")
    def test_binary_not_installed(self, mock_bin_dir, mock_which):
        mock_bin_dir.return_value = type(p := MagicMock())() if False else MagicMock()
        mock_bin_dir.return_value.__truediv__ = MagicMock(return_value=MagicMock())
        mock_bin_dir.return_value.__truediv__.return_value.is_file.return_value = False
        tunnel = CloudflareTunnel()
        assert tunnel.is_available() is False

    @patch("shutil.which", return_value="/usr/local/bin/cloudflared")
    def test_binary_in_path(self, mock_which):
        tunnel = CloudflareTunnel()
        assert tunnel.is_available() is True


class TestTunnelLifecycle:
    """测试隧道启停生命周期。"""

    @pytest.fixture
    def fake_process(self):
        """模拟 Popen 对象。"""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout = iter([])
        proc.stderr = None
        return proc

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_start_creates_process(self, mock_binary, mock_popen, fake_process):
        mock_popen.return_value = fake_process
        tunnel = CloudflareTunnel()
        tunnel.start(12000)

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "tunnel" in cmd
        assert "--url" in cmd
        assert "http://127.0.0.1:12000" in cmd
        assert "--no-autoupdate" in cmd
        assert tunnel.is_running() is True

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_stop_terminates_process(self, mock_binary, mock_popen, fake_process):
        mock_popen.return_value = fake_process
        tunnel = CloudflareTunnel()
        tunnel.start(12000)
        tunnel.stop()

        fake_process.terminate.assert_called_once()
        assert tunnel.is_running() is False

    @patch.object(CloudflareTunnel, "_find_binary", return_value=None)
    def test_start_without_binary_calls_error(self, mock_binary):
        tunnel = CloudflareTunnel()
        error_cb = MagicMock()
        tunnel.set_callbacks(on_error=error_cb)
        tunnel.start(12000)

        error_cb.assert_called_once()
        assert "not found" in error_cb.call_args[0][0].lower()


class TestUrlParsing:
    """测试从 cloudflared 输出解析 URL。"""

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_url_callback_fired(self, mock_binary, mock_popen):
        output_lines = iter([
            "INF Starting cloudflared\n",
            "INF |  https://test-tunnel-url.trycloudflare.com  |\n",
            "INF Registered tunnel connection\n",
            "",
        ])
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout = output_lines

        mock_popen.return_value = proc
        tunnel = CloudflareTunnel()
        url_cb = MagicMock()
        tunnel.set_callbacks(on_url=url_cb)
        tunnel.start(12000)
        tunnel._monitor_thread.join(timeout=2.0)

        url_cb.assert_called_once_with("https://test-tunnel-url.trycloudflare.com")

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_no_url_in_output(self, mock_binary, mock_popen):
        output_lines = iter([
            "INF Starting cloudflared\n",
            "ERR some error\n",
            "",
        ])
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stdout = output_lines

        mock_popen.return_value = proc
        tunnel = CloudflareTunnel()
        url_cb = MagicMock()
        tunnel.set_callbacks(on_url=url_cb)
        tunnel.start(12000)
        tunnel._monitor_thread.join(timeout=2.0)

        url_cb.assert_not_called()
        assert tunnel.get_url() is None


class TestCrashDetection:
    """测试进程崩溃检测。"""

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_on_stopped_callback_on_crash(self, mock_binary, mock_popen):
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stdout = iter(["",])

        mock_popen.return_value = proc
        tunnel = CloudflareTunnel()
        stopped_cb = MagicMock()
        tunnel.set_callbacks(on_stopped=stopped_cb)
        tunnel.start(12000)
        tunnel._monitor_thread.join(timeout=2.0)

        stopped_cb.assert_called_once()

    @patch("phonemic.tunnel.cloudflare.subprocess.Popen")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    def test_on_stopped_not_called_on_manual_stop(self, mock_binary, mock_popen):
        block = threading.Event()

        def blocking_lines():
            while not block.is_set():
                yield "\n"
            return

        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdout = blocking_lines()

        mock_popen.return_value = proc
        tunnel = CloudflareTunnel()
        stopped_cb = MagicMock()
        tunnel.set_callbacks(on_stopped=stopped_cb)
        tunnel.start(12000)
        tunnel.stop()
        block.set()
        tunnel._monitor_thread.join(timeout=2.0)

        stopped_cb.assert_not_called()
