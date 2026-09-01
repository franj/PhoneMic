"""
dispatcher 统一入口路由测试。

验证加密/明文模式的路径校验：
- 加密模式：根路由 404，仅 /{secret} 前缀放行（含尾斜杠归一化），防扫描
- 明文模式：仅白名单根路径放行，未知路径 404
- POST 等非 GET 方法由路由层返回 405
- 运行中替换 SecureChannel（算法切换）→ 新 secret 即时生效，无需重启
"""

import multiprocessing
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import set_bridge, start_server, stop_server, set_secure_channel
from phonemic.tunnel.e2ee import SecureChannel


_test_port_counter = 9500


def get_test_port():
    global _test_port_counter
    _test_port_counter += 1
    return _test_port_counter


def wait_for_server_ready(host, port, secret_path="", timeout=5.0):
    """探测入口路径返回 200 即视为就绪。"""
    prefix = f"/{secret_path}" if secret_path else ""
    url = f"http://{host}:{port}{prefix}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=0.5)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


def _get(host, port, path):
    """GET 请求，返回 (status, headers)；HTTP 错误码时返回 (code, headers)。"""
    try:
        resp = urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2)
        return resp.status, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


def _post(host, port, path):
    """POST 请求，返回状态码；HTTP 错误码时返回该码。"""
    req = urllib.request.Request(f"http://{host}:{port}{path}", method="POST", data=b"")
    try:
        resp = urllib.request.urlopen(req, timeout=2)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def enc_server():
    """加密模式服务器（带随机 secret_path，根路由 404）。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    host, port = "127.0.0.1", get_test_port()
    sc = SecureChannel(algorithm="auto")
    set_secure_channel(sc)
    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port, sc.secret_path)
    yield host, port, sc, bridge
    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


@pytest.fixture
def plain_server():
    """明文模式服务器（secret 为空，根路由可用）。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    host, port = "127.0.0.1", get_test_port()
    sc = SecureChannel(algorithm="none", mode="lan")
    set_secure_channel(sc)
    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port)
    yield host, port, sc, bridge
    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


class TestEncryptedPathGuard:
    """加密模式：根路由不可见，仅 /{secret} 前缀放行。"""

    def test_root_404(self, enc_server):
        host, port, sc, _ = enc_server
        status, _ = _get(host, port, "/")
        assert status == 404

    def test_secret_root_200(self, enc_server):
        host, port, sc, _ = enc_server
        status, headers = _get(host, port, f"/{sc.secret_path}/")
        assert status == 200
        assert "text/html" in headers.get("content-type", "")

    def test_secret_root_no_trailing_slash_200(self, enc_server):
        host, port, sc, _ = enc_server
        status, _ = _get(host, port, f"/{sc.secret_path}")
        assert status == 200

    def test_secret_subresources_200(self, enc_server):
        host, port, sc, _ = enc_server
        for path in ("/sodium.js", "/crypto_providers.js", "/favicon.ico"):
            status, _ = _get(host, port, f"/{sc.secret_path}{path}")
            assert status == 200, f"{path} 应返回 200"

    def test_wrong_secret_404(self, enc_server):
        host, port, sc, _ = enc_server
        status, _ = _get(host, port, "/wrongsecret/")
        assert status == 404

    def test_root_ws_404(self, enc_server):
        host, port, sc, _ = enc_server
        # 加密模式下根路径 WS 不可达（无 secret 前缀，握手 404）
        with pytest.raises(Exception):
            ws_connect(f"ws://{host}:{port}/ws")

    def test_secret_ws_handshake_ok(self, enc_server):
        host, port, sc, _ = enc_server
        # 握手成功即 ws_connect 不抛异常（HTTP 101）
        with ws_connect(f"ws://{host}:{port}/{sc.secret_path}/ws"):
            pass

    def test_post_method_405(self, enc_server):
        host, port, sc, _ = enc_server
        # add_get 注册：POST 在路由层直接 405，不进入 dispatcher
        assert _post(host, port, f"/{sc.secret_path}/") == 405
        assert _post(host, port, "/") == 405


class TestPlainPathGuard:
    """明文模式：根路由可用，未知路径 404。"""

    def test_root_200(self, plain_server):
        host, port, sc, _ = plain_server
        status, headers = _get(host, port, "/")
        assert status == 200
        assert "text/html" in headers.get("content-type", "")

    def test_known_resources_200(self, plain_server):
        host, port, sc, _ = plain_server
        for path in ("/sodium.js", "/crypto_providers.js", "/favicon.ico"):
            status, _ = _get(host, port, path)
            assert status == 200, f"{path} 应返回 200"

    def test_unknown_path_404(self, plain_server):
        host, port, sc, _ = plain_server
        status, _ = _get(host, port, "/some/random/path")
        assert status == 404

    def test_ws_root_handshake_ok(self, plain_server):
        host, port, sc, bridge = plain_server
        # none+LAN 无需 auth：连接即注册，触发 connect 事件
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            msg_type, _ = bridge.queue.get(timeout=2)
            assert msg_type == "connect"


def test_dynamic_secret_switch_without_restart():
    """运行中替换 SecureChannel：新 secret 即时生效，旧路径立即失效，无需重启。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    host, port = "127.0.0.1", get_test_port()

    sc1 = SecureChannel(algorithm="auto")
    set_secure_channel(sc1)
    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port, sc1.secret_path)
    assert _get(host, port, f"/{sc1.secret_path}/")[0] == 200

    # 运行中替换（模拟算法/模式切换），服务器不重启
    sc2 = SecureChannel(algorithm="auto")
    set_secure_channel(sc2)
    time.sleep(0.3)

    assert sc1.secret_path != sc2.secret_path
    assert _get(host, port, f"/{sc1.secret_path}/")[0] == 404   # 旧路径立即失效
    assert _get(host, port, f"/{sc2.secret_path}/")[0] == 200   # 新路径可用

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)
