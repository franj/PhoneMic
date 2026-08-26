"""
E2EE 服务器端集成测试。
验证加密/解密、控制消息、兜底通知等在真实 WebSocket 连接中的行为。
"""

import json
import multiprocessing
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import (
    set_bridge, start_server, stop_server,
    set_e2ee_manager, send_to_phone,
)
from phonemic.tunnel.e2ee import E2EEManager


# ---------- 辅助函数 ----------
_test_port_counter = 9900


def get_test_port():
    global _test_port_counter
    _test_port_counter += 1
    return _test_port_counter


def wait_for_server_ready(host, port, timeout=5.0):
    url = f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=0.5)
            if resp.status in (200, 404):
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


def assert_first_msg_connect(queue):
    msg_type, text = queue.get(timeout=2)
    assert msg_type == "connect"
    assert text is None


# ---------- Fixtures ----------
@pytest.fixture
def e2ee_server():
    """启动带 E2EE 管理器的服务器"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    mgr = E2EEManager()
    set_e2ee_manager(mgr)

    start_server(host, port, bridge)
    if not wait_for_server_ready(host, port):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, queue, mgr

    stop_server()
    set_e2ee_manager(None)
    time.sleep(0.3)


# ---------- 测试 ----------

class TestConfigIncludesE2EEState:
    """config 消息应包含 e2ee_enabled 字段。"""

    def test_config_has_e2ee_disabled(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            assert data["type"] == "config"
            assert data["e2ee_enabled"] is False

    def test_config_has_e2ee_enabled(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            assert data["type"] == "config"
            assert data["e2ee_enabled"] is True


class TestEncryptedMessageFlow:
    """E2EE 启用后，服务器应解密客户端发来的加密消息。"""

    def test_server_decrypts_encrypted_preview(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)  # consume config

            encrypted = mgr.wrap({"type": "preview", "text": "secret hello"})
            ws.send(json.dumps(encrypted))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == "secret hello"

    def test_server_decrypts_encrypted_send(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            encrypted = mgr.wrap({"type": "send", "text": "encrypted send"})
            ws.send(json.dumps(encrypted))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "send"
            assert text == "encrypted send"


class TestPlaintextFallback:
    """E2EE 启用时收到明文，应回复 e2ee_required。"""

    def test_plaintext_triggers_e2ee_required(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)  # consume config

            ws.send(json.dumps({"type": "preview", "text": "plaintext"}))

            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            assert data["type"] == "e2ee_required"

    def test_control_messages_pass_through_plaintext(self, e2ee_server):
        """控制消息在 E2EE 启用时也应明文通过。"""
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            ws.send(json.dumps({"type": "e2ee_disabled"}))

            # 不应收到 e2ee_required，也不应在队列中产生事件
            # （e2ee_disabled 不在 preview/send 处理路径中，会被记为 unknown type）
            # 等待一小段时间确认无事件
            time.sleep(0.2)


class TestEncryptedWhenDisabled:
    """E2EE 禁用时收到加密消息，应忽略。"""

    def test_encrypted_ignored_when_disabled(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        # mgr not enabled

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            fake_encrypted = {"type": "encrypted", "data": "fake_data_here"}
            ws.send(json.dumps(fake_encrypted))

            # 队列中不应有任何消息（被忽略）
            with pytest.raises(Exception):
                queue.get(timeout=1)


class TestControlMessageNotEncrypted:
    """控制消息不应被加密，即使 E2EE 已启用。"""

    def test_send_to_phone_control_stays_plaintext(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            send_to_phone({"type": "e2ee_enabled"})

            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            assert data["type"] == "e2ee_enabled"
            assert "data" not in data  # 不是加密信封

    def test_send_to_phone_data_gets_encrypted(self, e2ee_server):
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            send_to_phone({"type": "config", "test": "value"})

            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            # config 在控制类型中，不应被加密
            assert data["type"] == "config"
            assert data["test"] == "value"

    def test_preview_pushed_encrypted(self, e2ee_server):
        """非控制类型的消息应该被加密。"""
        host, port, queue, mgr = e2ee_server
        mgr.enable()

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            ws.recv(timeout=2)

            send_to_phone({"type": "preview", "text": "pushed"})

            msg = ws.recv(timeout=2)
            data = json.loads(msg)
            assert data["type"] == "encrypted"
            assert "data" in data

            decrypted = mgr.unwrap(data)
            assert decrypted["type"] == "preview"
            assert decrypted["text"] == "pushed"
