"""
隧道保活。

Cloudflare 快隧道（trycloudflare）只靠「真实 HTTP 流量」维持存活：实测在无人
访问时空闲 23~33 分钟后，CF 侧会注销该 quick tunnel 实例，域名随之解析失败
（NXDOMAIN），且 cloudflared 再也注册不回去（日志：`Unauthorized: Tunnel not
found`）。连接层自身的保活（TCP keepalive / QUIC ping）挡不住这件事——换协议
实测同样失效，能决定生死的只有「有没有 HTTP 请求真的打到这个域名上」。

本模块周期性地从本机向公网入口发一个极小的 GET，让隧道持续有流量。

**它只能续命，不能复活**：域名一旦被 CF 回收，请求什么都唤不回来，此时唯一的
出路是重启 cloudflared 拿一个新域名（手机端需重新扫码）。因此本模块一半的价值
在于「尽早发现并告知用户」，而非「自动修好」。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: 探测间隔（秒）。实测失效窗口在 20 分钟以上，60s 有 20 倍余量。
DEFAULT_INTERVAL = 60.0

#: 单次请求超时（秒）。
DEFAULT_TIMEOUT = 10.0

#: 连续失败多少次才判定「公网入口不可达」。单次失败可能只是本机网络抖动，或
#: 域名刚下发尚未生效（cloudflared 提示 "it may take some time to be
#: reachable"），因此不因一次失败就打扰用户。
DEFAULT_FAIL_THRESHOLD = 3

#: 保活落点。用专用端点而非复用 /api/lang.json 或打一个不存在的路径：前者是
#: 静态资源、将来若加缓存头就会让流量不穿透隧道；后者语义会漂，且与「非法路径
#: 返回 404」的防扫描设计冲突（见 api._normalize_path）。
KEEPALIVE_PATH = "/api/keepalive"

#: 期望的响应状态字段值。必须校验正文——Cloudflare 在隧道失效时也会返回 200 或
#: 4xx 的错误页，只看状态码会被骗过去。
_EXPECTED_STATUS = "ok"

#: 响应体读取上限（字节）。实际正文约 40 字节，留足余量即可。
_MAX_BODY_BYTES = 256

#: 隧道确定已死的状态码，源自 cloudflared / trycloudflare 的实际行为。
_DEAD_STATUS = {502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}


class TunnelKeepalive:
    """周期性探测隧道公网入口，维持流量并在可达性翻转时上报。

    线程模型：start() 拉起一个 daemon 线程，stop() 通过 Event 中断等待并回收。
    线程自持探测状态（失败计数、是否已上报不可达），因此 stop() 之后残留的旧
    线程不会污染新一轮的状态。全程只用标准库 urllib 在**独立线程**里发请求，
    不接触 asyncio 事件循环，不会干扰服务端。
    """

    def __init__(
        self,
        on_state_change: Optional[Callable[[bool], None]] = None,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
    ):
        """
        Args:
            on_state_change: 可达性**翻转**时的回调，参数 reachable。
                             False = 连续失败达阈值，公网入口不可达；
                             True  = 恢复可达。
                             只在翻转时触发一次，不是每次探测都回调。
            interval: 探测间隔（秒）。
            timeout: 单次请求超时（秒）。
            fail_threshold: 连续失败多少次判定不可达。
        """
        self._on_state_change = on_state_change
        self._interval = interval
        self._timeout = timeout
        self._fail_threshold = fail_threshold

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._url: Optional[str] = None

    # ---- 生命周期 ----

    def start(self, url: str, secret_path: str = "") -> None:
        """启动保活（会先停掉上一轮）。

        Args:
            url: 隧道公网地址，如 https://xxx.trycloudflare.com（可带尾斜杠）。
            secret_path: 加密模式下的随机入口前缀；明文模式传空串。
        """
        self.stop()

        probe_base = _build_probe_base(url, secret_path)
        if probe_base is None:
            logger.warning(f"Keepalive not started: unusable tunnel url {url!r}")
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._loop,
            args=(probe_base, stop_event),
            name="tunnel-keepalive",
            daemon=True,
        )
        with self._lock:
            self._stop_event = stop_event
            self._url = probe_base
            self._thread = thread
        thread.start()
        logger.info(
            f"Tunnel keepalive started: {probe_base} (every {self._interval:g}s)"
        )

    def stop(self) -> None:
        """停止保活并等待线程退出（最多 2 秒，避免拖慢模式切换）。"""
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            self._thread = None
            self._url = None
        stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def is_running(self) -> bool:
        """保活线程是否在运行。"""
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    # ---- 内部 ----

    def _loop(self, probe_base: str, stop_event: threading.Event) -> None:
        """探测循环：先立即探一次，之后按 interval 对齐节拍（不叠加请求耗时）。"""
        fail_count = 0
        unreachable = False

        while not stop_event.is_set():
            started = time.time()
            try:
                ok = self._probe(probe_base)
            except Exception as e:  # 探测本身不允许让线程死掉
                logger.exception(f"Keepalive probe crashed: {e}")
                ok = False
            if stop_event.is_set():
                break

            if ok:
                if unreachable:
                    unreachable = False
                    logger.info("Tunnel keepalive: public endpoint reachable again")
                    self._notify(True)
                fail_count = 0
            else:
                fail_count += 1
                if not unreachable and fail_count >= self._fail_threshold:
                    unreachable = True
                    logger.warning(
                        "Tunnel keepalive: public endpoint unreachable "
                        f"({fail_count} consecutive failures)"
                    )
                    self._notify(False)
                else:
                    logger.debug(
                        f"Tunnel keepalive: probe failed "
                        f"({fail_count}/{self._fail_threshold})"
                    )

            elapsed = time.time() - started
            stop_event.wait(max(0.0, self._interval - elapsed))

    def _notify(self, reachable: bool) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(reachable)
        except Exception as e:
            logger.exception(f"Keepalive state callback failed: {e}")

    def _probe(self, probe_base: str) -> bool:
        """探测一次，返回「公网入口可达且源站确实处理了本次请求」。

        URL 每次带毫秒时间戳，一物两用：既做缓存击穿（让 URL 唯一），也作为
        nonce 交给服务端回显。服务端把 t 原样写回响应体，这里比对一致才算通过
        ——只有真正由源站为**本次**请求生成的响应能对上，被缓存或重放的旧响应
        对不上，于是「保活静默失效」会变成「保活明确报错」。
        """
        nonce = str(int(time.time() * 1000))
        req = urllib.request.Request(
            f"{probe_base}?t={nonce}",
            headers={
                "User-Agent": "PhoneMic-keepalive/1.0",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    return False
                return _verify_body(resp.read(_MAX_BODY_BYTES), nonce)
        except urllib.error.HTTPError as e:
            # HTTPError 是 URLError 的子类，必须先捕获。4xx/5xx 大多说明请求已经
            # 穿透到源站（隧道是活的），只有 CF 的「隧道不存在」页才算不可达。
            body = b""
            try:
                body = e.read(256)
            except Exception:
                pass
            if e.code in _DEAD_STATUS:
                return False
            if e.code == 404 and (b"1033" in body or b"tunnel" in body.lower()):
                return False
            return True
        except urllib.error.URLError:
            # DNS 解析失败（域名已被回收）或连不上 Cloudflare 边缘
            return False
        except Exception:
            return False


def _verify_body(body: bytes, nonce: str) -> bool:
    """校验响应体确实是源站为「本次请求」（nonce）生成的。

    两道检查缺一不可：

    - ``status == "ok"``：证明请求穿透到了 PhoneMic 源站（CF 错误页没有该字段）；
    - ``t == nonce``：证明响应没有被缓存或重放。命中缓存时请求根本没进隧道，
      保活是无效的——这种情况必须判失败并上报，否则又变回静默失效。
    """
    try:
        data = json.loads(body)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return data.get("status") == _EXPECTED_STATUS and data.get("t") == nonce


def _build_probe_base(url: str, secret_path: str) -> Optional[str]:
    """拼出探测基址（不含时间戳参数）；url 不可用时返回 None。

    加密模式下所有资源都位于 /{secret_path}/ 前缀之下（见 api._normalize_path），
    因此探测路径也要带上前缀，否则只会拿到 404。
    """
    if not url:
        return None
    base = url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    prefix = f"/{secret_path}" if secret_path else ""
    return f"{base}{prefix}{KEEPALIVE_PATH}"
