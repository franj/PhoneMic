"""
TunnelKeepalive 单元测试。

用真实的本机 HTTP 服务端模拟源站（而不是 mock urllib），
让 URL 构造、状态码判定、正文校验都走真实链路。
"""

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from phonemic.tunnel.keepalive import (
    TunnelKeepalive,
    _build_probe_base,
    _verify_body,
)

from conftest import get_test_port


def _wait_for(predicate, timeout=5.0):
    """轮询等待条件成立。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _State:
    """本机假源站的可调状态。"""

    def __init__(self):
        self.status = 200
        # None = 按请求里的 t 正常回显（与 api._serve_keepalive 行为一致）；
        # 显式赋值则固定返回该正文，用于模拟 CF 错误页 / 缓存命中等异常。
        self.body = None
        self.paths = []


class _Handler(BaseHTTPRequestHandler):
    """模拟源站：默认把请求的 t 原样回显，忽略路径本身（路径由断言检查）。"""

    def do_GET(self):
        state = self.server.state
        state.paths.append(self.path)
        if state.body is None:
            nonce = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            body = json.dumps({"status": "ok", "t": nonce}).encode()
        else:
            body = state.body
        self.send_response(state.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def probe_server():
    """本机假源站。顺带禁用代理，避免系统代理把 127.0.0.1 请求截走。"""
    port = get_test_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    state = _State()
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        urllib.request.install_opener(urllib.request.build_opener())
        httpd.shutdown()
        httpd.server_close()


class TestBuildProbeBase:
    """探测地址构造（加密模式需带 secret 前缀）。"""

    def test_plain_mode(self):
        assert _build_probe_base("https://x.trycloudflare.com", "") == \
            "https://x.trycloudflare.com/api/keepalive"

    def test_encrypted_mode_keeps_secret_prefix(self):
        assert _build_probe_base("https://x.trycloudflare.com", "abc123") == \
            "https://x.trycloudflare.com/abc123/api/keepalive"

    def test_strips_trailing_slash(self):
        assert _build_probe_base("https://x.trycloudflare.com/", "") == \
            "https://x.trycloudflare.com/api/keepalive"

    @pytest.mark.parametrize("bad", ["", "   ", "x.trycloudflare.com", "ftp://x/y"])
    def test_rejects_unusable_url(self, bad):
        assert _build_probe_base(bad, "") is None


class TestVerifyBody:
    """响应体校验：状态字段 + nonce 回显，两道都要过。"""

    def test_accepts_matching_nonce(self):
        assert _verify_body(b'{"status":"ok","t":"123"}', "123") is True

    def test_rejects_mismatched_nonce(self):
        """缓存 / 重放的旧响应——请求没进隧道，必须判失败。"""
        assert _verify_body(b'{"status":"ok","t":"122"}', "123") is False

    def test_rejects_missing_nonce(self):
        """服务端未回显（如旧版本源站）也算失败，避免升级期误判。"""
        assert _verify_body(b'{"status":"ok"}', "123") is False

    def test_rejects_non_ok_status(self):
        assert _verify_body(b'{"status":"nope","t":"123"}', "123") is False

    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"<html>cloudflare error page</html>",
            b'{"status":"ok","t":123}',  # t 必须是字符串
            b'["status","ok"]',  # 顶层不是对象
        ],
    )
    def test_rejects_malformed_body(self, body):
        assert _verify_body(body, "123") is False


class TestProbeBehaviour:
    """探测行为与状态上报。"""

    def test_hits_keepalive_path_with_cache_buster(self, probe_server):
        base, state = probe_server
        ka = TunnelKeepalive(interval=0.05, timeout=2.0)
        ka.start(base)
        try:
            assert _wait_for(lambda: len(state.paths) >= 1)
            assert state.paths[0].startswith("/api/keepalive?t=")
        finally:
            ka.stop()

    def test_initial_success_reports_nothing(self, probe_server):
        """首次就正常不应打扰用户——只有可达性翻转才回调。"""
        base, state = probe_server
        states = []
        ka = TunnelKeepalive(on_state_change=states.append, interval=0.05, timeout=2.0)
        ka.start(base)
        try:
            assert _wait_for(lambda: len(state.paths) >= 3)
            assert states == []
        finally:
            ka.stop()

    def test_unreachable_after_threshold_then_recovers(self, probe_server):
        base, state = probe_server
        states = []
        ka = TunnelKeepalive(
            on_state_change=states.append, interval=0.05, timeout=2.0, fail_threshold=2
        )
        state.status = 502
        ka.start(base)
        try:
            assert _wait_for(lambda: states == [False]), f"states={states}"
            state.status = 200
            assert _wait_for(lambda: states == [False, True]), f"states={states}"
        finally:
            ka.stop()

    def test_body_without_ok_is_treated_as_failure(self, probe_server):
        """200 但正文不是保活响应（例如 CF 错误页）不能算成功。"""
        base, state = probe_server
        state.status = 200
        state.body = b"<html>cloudflare error page</html>"
        states = []
        ka = TunnelKeepalive(
            on_state_change=states.append, interval=0.05, timeout=2.0, fail_threshold=2
        )
        ka.start(base)
        try:
            assert _wait_for(lambda: states == [False]), f"states={states}"
        finally:
            ka.stop()

    def test_cached_response_is_treated_as_failure(self, probe_server):
        """缓存命中的旧响应（t 对不上）同样判失败——此时请求没进隧道，保活无效。"""
        base, state = probe_server
        state.body = b'{"status":"ok","t":"1"}'
        states = []
        ka = TunnelKeepalive(
            on_state_change=states.append, interval=0.05, timeout=2.0, fail_threshold=2
        )
        ka.start(base)
        try:
            assert _wait_for(lambda: states == [False]), f"states={states}"
        finally:
            ka.stop()

    def test_connection_refused_is_unreachable(self):
        """域名解析失败 / 连不上边缘 → 判不可达，且只上报一次。"""
        dead_port = get_test_port()
        states = []
        ka = TunnelKeepalive(
            on_state_change=states.append, interval=0.05, timeout=1.0, fail_threshold=1
        )
        ka.start(f"http://127.0.0.1:{dead_port}")
        try:
            assert _wait_for(lambda: states == [False]), f"states={states}"
            time.sleep(0.3)
            assert states == [False], "同一状态不应重复上报"
        finally:
            ka.stop()


class TestLifecycle:
    """线程生命周期。"""

    def test_stop_terminates_thread(self, probe_server):
        base, _ = probe_server
        ka = TunnelKeepalive(interval=10.0, timeout=2.0)
        ka.start(base)
        assert ka.is_running() is True
        ka.stop()
        assert ka.is_running() is False

    def test_stop_without_start_is_safe(self):
        TunnelKeepalive().stop()

    def test_restart_replaces_previous_thread(self, probe_server):
        base, _ = probe_server
        ka = TunnelKeepalive(interval=0.05, timeout=2.0)
        ka.start(base)
        first = ka._thread
        ka.start(base)
        try:
            assert ka._thread is not first
            assert first.is_alive() is False
        finally:
            ka.stop()

    def test_unusable_url_does_not_start(self):
        ka = TunnelKeepalive()
        ka.start("not-a-url")
        assert ka.is_running() is False
