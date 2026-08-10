"""
PhoneMic 后端服务单元测试
使用真实 HTTP/WebSocket 连接测试 Tremolo 服务器。
"""

import json
import multiprocessing
import re
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import set_bridge, start_server, stop_server


# ---------- 辅助函数 ----------
_test_port_counter = 8880


def get_test_port():
    global _test_port_counter
    _test_port_counter += 1
    return _test_port_counter


def wait_for_server_ready(host, port, timeout=5.0):
    """等待服务器就绪"""
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
def server_with_bridge():
    """启动真实服务器并返回 (host, port, queue)，测试结束后自动停止"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, queue

    stop_server()
    time.sleep(0.3)  # 等待端口释放


# ---------- 测试 WebSocket 消息处理 ----------
def test_websocket_message_parsing(server_with_bridge):
    """验证 WebSocket 消息能正确解析并推送到队列"""
    host, port, queue = server_with_bridge

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        assert_first_msg_connect(queue)

        ws.send(json.dumps({"type": "preview", "text": "hello"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "hello"

        ws.send(json.dumps({"type": "send", "text": "world"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "send"
        assert text == "world"


def test_websocket_invalid_json(server_with_bridge):
    """无效 JSON 不应崩溃，应继续接收后续消息"""
    host, port, queue = server_with_bridge

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        assert_first_msg_connect(queue)

        ws.send("this is not json")

        ws.send(json.dumps({"type": "preview", "text": "after bad"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "after bad"


def test_connection_lifecycle(server_with_bridge):
    """测试连接/断开事件是否正确推送"""
    host, port, queue = server_with_bridge

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        event, _ = queue.get(timeout=2)
        assert event == "connect"

    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_only_one_active_connection(server_with_bridge):
    """新连接应自动替换旧连接"""
    host, port, queue = server_with_bridge

    ws1 = ws_connect(f"ws://{host}:{port}/ws")
    event, _ = queue.get(timeout=2)
    assert event == "connect"

    ws2 = ws_connect(f"ws://{host}:{port}/ws")
    events = []
    for _ in range(2):
        event, _ = queue.get(timeout=2)
        events.append(event)
    assert "disconnect" in events
    assert "connect" in events

    ws1.close()
    ws2.close()


def test_config_message_on_connect(server_with_bridge):
    """连接后客户端应收到 config 消息，包含 mobile_max_records 配置"""
    host, port, queue = server_with_bridge

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        # 先消费队列中的 connect 事件
        assert_first_msg_connect(queue)

        # 客户端应收到 config 消息
        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "config"
        assert "mobile_max_records" in data
        assert isinstance(data["mobile_max_records"], int)


def test_unknown_message_type_tolerance(server_with_bridge):
    """未知消息类型不应崩溃，后续合法消息应正常处理"""
    host, port, queue = server_with_bridge

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        assert_first_msg_connect(queue)

        # 消费 config 消息
        ws.recv(timeout=2)

        # 发送未知类型
        ws.send(json.dumps({"type": "unknown_type", "text": "???"}))
        time.sleep(0.3)  # 等待服务器处理

        # 后续合法消息应正常工作
        ws.send(json.dumps({"type": "preview", "text": "still working"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "still working"


# ---------- 测试 HTTP 路由 ----------
def test_get_root_returns_html(server_with_bridge):
    """GET / 应返回 HTML 响应"""
    host, port, _ = server_with_bridge

    url = f"http://{host}:{port}/"
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.read().decode("utf-8")
    assert len(body) > 0


def test_root_html_i18n_replaced(server_with_bridge):
    """GET / 返回的 HTML 中 __I18N_JSON__ 占位符应被替换为有效 JSON"""
    host, port, _ = server_with_bridge

    url = f"http://{host}:{port}/"
    resp = urllib.request.urlopen(url, timeout=2)
    body = resp.read().decode("utf-8")

    # 原始占位符不应出现
    assert "__I18N_JSON__" not in body, "i18n placeholder was not replaced"

    # 应包含 i18n-data script 标签，且内容为有效 JSON 对象
    match = re.search(
        r'<script id="i18n-data" type="application/json">\s*(.*?)\s*</script>',
        body, re.DOTALL
    )
    assert match is not None, "i18n-data script tag not found in HTML"
    i18n_data = json.loads(match.group(1))
    assert isinstance(i18n_data, dict)
    assert len(i18n_data) > 0, "i18n data should not be empty"


def test_favicon_route(server_with_bridge):
    """GET /favicon.ico 应返回图标文件"""
    host, port, _ = server_with_bridge

    url = f"http://{host}:{port}/favicon.ico"
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    content_type = resp.headers.get("content-type", "")
    assert "image" in content_type, f"Expected image content-type, got: {content_type}"
    body = resp.read()
    assert len(body) > 0, "favicon body should not be empty"


# ---------- 集成测试 ----------
def test_real_server_with_websocket_client():
    """启动真实服务，使用同步 WebSocket 客户端测试多种消息"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port):
        stop_server()
        pytest.fail("Server did not start within timeout")

    test_msgs = [
        ["preview", "integration test"],
        ["send", "你好世界 🌍😊 日本語 漢字"],
        ("preview", "Hello, PhoneMic!"),
        ("preview", ""),                     # 空字符串应正常处理
        ("preview", "A" * 11000),            # 超长文本 (>10KB)
        ("preview", "🐍✨ 混合符号 ¥€$ 测试"),
        ("preview", "  前后空格  "),          # 空格保留
        ("preview", "\n\t多行文本\n第二行"),  # 转义字符保留
        ("send", "普通发送"),
        ("send", "超长发送" + "B" * 15000),
        ("send", "表情包 😀😂😍"),
        ("send", ""),                       # 空发送
        ("send", "  修剪测试  "),            # 空格原样传递
    ]

    try:
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            assert_first_msg_connect(queue)
            for orig_type, orig_text in test_msgs:
                ws.send(json.dumps({"type": orig_type, "text": orig_text}))
                msg_type, text = queue.get(timeout=2)
                assert msg_type == orig_type
                assert text == orig_text
    finally:
        stop_server()

    # 断开后应收到 disconnect 事件
    msg_type, _ = queue.get(timeout=2)
    assert msg_type == "disconnect"


def test_server_start_stop():
    """验证 start_server / stop_server 能正常启停且释放端口"""
    host = "127.0.0.1"
    port = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())

    start_server(host, port, bridge)

    url = f"http://{host}:{port}/"
    assert wait_for_server_ready(host, port), "Server did not start"

    stop_server()
    time.sleep(0.5)

    # 验证端口已释放（连接应失败）
    with pytest.raises((urllib.error.URLError, OSError, ConnectionRefusedError)):
        urllib.request.urlopen(url, timeout=1.0)


def test_server_restart_cycle():
    """验证服务器 stop 后可以重新 start（模拟网络切换场景）"""
    host = "127.0.0.1"
    port = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)

    # 第一次启动
    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port), "First start failed"

    # 停止
    stop_server()
    time.sleep(0.5)

    # 第二次启动（复用同一端口，验证 _create_app() 工厂模式）
    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port), "Restart failed"

    # 验证功能正常
    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        assert_first_msg_connect(bridge.queue)

        ws.send(json.dumps({"type": "preview", "text": "after restart"}))
        msg_type, text = bridge.queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "after restart"

    stop_server()
    time.sleep(0.3)
