"""上传会话表 + 分片状态机 + 验签（docs/http-upload-design.md）。

一次上传 = 一条 ``UploadSession``。数据全走 HTTP ``PUT /api/upload/<sid>``，
WS 只在两头出现：协商（``upload_begin`` / ``upload_ready``）与取消
（``upload_cancel``，单向、幂等、无回帧）。

本模块只做三件事，全都不碰 HTTP 框架：

  1. **会话表**：建会话、按连接归属、TTL、统一作废入口（``abort_session``）
  2. **验签**：``X-Pm-Mac``（keyed BLAKE2b，见 ``tunnel/crypto/mac.py``）
  3. **分片状态机**：offset 三行判定 → 解密 → 写盘 → 收尾

HTTP 的收发与响应构造在 ``api.py::_serve_upload``，它只从这里取「决定」与「状态」。

几条不能写反的规则（写反了不报错、只在不常见路径上出事）：

- **只有 401 那一类不碰会话**（验签没过 / 会话不存在）。其余非 200 一律作废
  （§5.2 的 ⚠️）——否则 LAN 下知道 ``sid`` 的人发一个签名错的 PUT 就能毁掉别人
  正在传的文件（那是纯 DoS）。
- **重复片只能「忽略」不能「作废」**（§5.5 红线）：否则录下一片重放即可 DoS。
- **判定顺序固定为「验签 → offset 判定 → 解密」**（§5.8 前提 1）：只有
  ``offset == received`` 的片才允许进解密。写反了不报错，只在「有人重放一片」时
  静默变成「解密失败 ⇒ 作废上传」，比「忽略」差一档。
- **「解密成功但写盘失败」不能靠回退 ``received`` 重来**：``decrypt()`` 一成功，
  provider 的 ``_rx_seq`` 就前进且不可回退，回退会让它与 ``received`` 永久错位、
  后面每一片都解不开。正确做法是作废会话（§5.8 前提 3）。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from phonemic.gui.file import FileSink, sanitize_name
from phonemic.gui.photo import PhotoSink
from phonemic.tunnel.crypto import create_provider, decode_mac, upload_mac
from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.errors import CryptoError
logger = logging.getLogger(__name__)

# ---------- 常量 ----------

# 服务端建议的片大小（明文字节）。随 ``upload_ready`` 下发，将来调参不用改手机页面。
#
# 为什么是 15MB：Cloudflare 免费/Pro 计划单请求体上限 100MB ⇒ 必须分片，15MB 留了
# 6 倍余量；而吞吐 ≈ 片大小 ÷ 每片来回时间，相对旧实现（256KB）把 RTT 部分摊销掉
# 60 倍。片越大服务端「整片收齐才能解密」占的内存越大，这是唯一的反向约束。
UPLOAD_CHUNK_SIZE = 15 * 1024 * 1024

# photo 强制上限：图片必须整图驻留内存才能写剪贴板，必须在 ① 阶段就拒（内存炸弹）
PHOTO_MAX_SIZE = 32 * 1024 * 1024

# 会话 TTL：每次收到**合法**分片就向后顺延。连接级作废（WS 关闭）是主力，
# TTL 只兜「连接还活着但谁也不动了」以及服务端自己重启这类边角。
SESSION_TTL = 300.0

# sid 随机字节数：它是路径里唯一的选择器，必须真随机（§4.2）
SID_BYTES = 24

# 每片密文的固定开销 = nonce(24) + 明文前 8 字节片序号 + tag(16) = 48。
#
# 两种算法完全一致（片序号统一为明文前缀，§5.8）⇒ 线上长度恒等于
# ``X-Pm-Len + 48``，可精确预计算、不需要 chunked 编码。
# ⚠️ 那 8 字节虽然由 provider 内部打上/剥掉，**但服务端必须把它算进来**：
# 少了它 ⇒ 每一片合法请求都会被 Content-Length 校验挡成 400（而且是"自己人
# 状态不对"那一档，会顺带把会话作废），读 body 的封顶也会少 8 字节。
NONCE_SIZE = 24
SEQ_PREFIX_SIZE = 8
TAG_SIZE = 16
CHUNK_OVERHEAD = NONCE_SIZE + SEQ_PREFIX_SIZE + TAG_SIZE

# 密钥材料长度：一次 CSPRNG 出 64 字节切两半（§5.3「两把钥匙，别用一把兼两职」）
_KEY_MATERIAL_SIZE = 64
_KEY_SIZE = 32

# 片内进度帧的节流参数（§5.13）：**只按恒定 tick**，不再叠「变化量阈值」。
#
# 09-23 起进度条旁边要显示 10 秒平均速率，采样必须**规律**：叠了阈值的话，慢链路上
# 「推进不足一个千分位」就不发帧，速率会**冻在旧值**上（看着像还在传，其实早卡住了）。
# 帧本身约 60 字节，10 帧/秒 ≈ 600B/s，走的是 PC → 手机方向（被 CF 整形的是手机上
# 行）⇒ 开销可忽略。
PROGRESS_TICK = 0.1

VALID_REFS = ("file", "photo")


# ---------- 判定结果 ----------


@dataclass(frozen=True)
class ChunkVerdict:
    """一片的处置结果。

    ``action`` 有三个取值，对应 §5.5 的「三个出口」：

    - ``"write"``  —— 正常推进：调用方必须读 body 并交给 ``commit_chunk()``
    - ``"ignore"`` —— 重复片 / 已收尾的会话：回 200，**不读密文、不解密、不作废**
    - ``"abort"``  —— 回 ``status`` 并作废会话

    ⚠️ ``judge()`` 返回 ``"abort"`` 时**不能自己调** ``abort_session()`` 之外的东西，
    而 ``commit_chunk()`` 返回 ``"abort"`` 时**必须由调用方**调 —— 它持有会话锁，
    再去拿同一把锁会死锁。
    """

    action: str
    status: int
    received: int
    abort: bool = False
    done: bool = False
    saved: str = ""
    reason: str = ""


# ---------- 进度（片内） ----------


class ChunkProgressThrottle:
    """按**恒定 tick** 节流片内进度帧（§5.13）。

    一次上传一个实例，活在 ``_read_upload_body`` 的读循环里 —— 进度点必须在**读循环**
    中产生，不能挂在 ``commit_chunk`` 上：后者是「整片收完、一次性 ``received += 15MB``」
    的阶梯，拿它当进度就只在整片落地时跳一格，而 CF 上一片要两三分钟（§5.13 那张表）。

    ⚠️ **不叠「变化量阈值」**（09-23）：进度条旁边要显示 10 秒平均速率，采样必须规律
    —— 有阈值时慢链路上「推进不足一个千分位」就不发帧，速率会**冻在旧值**上。

    发送走注入的 ``send``（``async (sid, received)``），本类不 import ``api.py`` ——
    那边反过来要 import 本模块，直接引用会成环。

    ⚠️ **尽力而为**：``send`` 抛异常（连接已关是常态）就地停发、绝不冒泡。它是从
    HTTP handler 里调的，异常冒出去就是一屏 ASGI 栈（§6.7 第 1 条同族）。
    """

    def __init__(
        self,
        sid: str,
        send: Callable[[str, int], Any],
        tick: float = PROGRESS_TICK,
    ) -> None:
        self._sid = sid
        self._send = send
        self._tick = tick
        self._last_ts: Optional[float] = None   # None ⇒ 首帧不设时间门槛
        self.enabled = True

    async def note(self, received: int) -> bool:
        """报告「已收到 ``received`` 字节」；真发出去了一帧则返回 True。

        ``received`` 是**会话累计**（已落盘的 + 当前片缓冲区里已到的），不是本片长度。
        """
        if not self.enabled:
            return False
        now = time.monotonic()
        if self._last_ts is not None and now - self._last_ts < self._tick:
            return False
        self._last_ts = now
        try:
            await self._send(self._sid, received)
        except Exception as e:
            # 对端已关 / 连接不可用：停发即可，绝不冒泡（§6.7 第 1 条同族）
            self.enabled = False
            logger.debug("进度帧发送失败，停止推送: sid=%s - %s", self._sid, e)
            return False
        return True


# ---------- 会话 ----------


class UploadSession:
    """一次上传的完整状态：声明信息、密钥、provider、sink、进度。

    ``provider`` 是**以 ``k_body`` 单独创建**的实例，与跑 WS 的那个分开——seq 是
    单个计数器，两条独立 TCP 的到达顺序不由发送方决定，共用实例必然出现
    「必有一条解不开」的概率性失败（§5.8「为什么必须是另一个实例」）。
    """

    def __init__(
        self,
        *,
        sid: str,
        kind: str,
        name: str,
        size: int,
        conn_id: int,
        websocket: Any,
        algorithm: str,
        k_mac: bytes,
        k_body: bytes,
        provider: CryptoProvider,
        sink: Any,
        expires_at: float,
        liveness: Any = None,
    ) -> None:
        self.sid = sid
        self.kind = kind
        self.name = name
        self.size = size
        self.conn_id = conn_id
        self.websocket = websocket
        self.algorithm = algorithm
        self.k_mac: Optional[bytes] = k_mac
        self.k_body: Optional[bytes] = k_body
        self.provider: Optional[CryptoProvider] = provider
        self.sink = sink
        self.expires_at = expires_at
        # WS 连接的判活状态：HTTP 活动也要刷新它，否则一条健康的大片上传会在
        # 60s 时被判死、然后被「WS 关闭 ⇒ 会话作废」这条规则杀掉（§6.5，必做）
        self.liveness = liveness

        self.received = 0
        self.done = False
        self.result: Dict[str, Any] = {}

        # 作废标记：PUT 的读循环据此停止写盘（但**继续读**，见 §5.6.3 约束 4）。
        # 必须放会话里而不是挂连接对象上：HTTP 请求与 WS 是两个独立 handler，
        # 它们之间唯一的共享物就是这张会话表。
        self.abort_event = asyncio.Event()
        # 按 sid 的锁：只包住「解密 + 写盘」那一小段，串行化并发片与作废
        self.lock = asyncio.Lock()

    # ---- 只读属性 ----

    @property
    def saved(self) -> str:
        """服务端分配好的最终文件名（重名已改号）。"""
        return getattr(self.sink, "saved", "") or ""

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    # ---- 密钥卫生（§5.9 方案二的专属要求） ----

    def drop_body_key(self) -> None:
        """摘掉 body 密钥与 provider。

        收尾后调用：会话还要留到过期（好让「最后一片的响应丢了、客户端重发」
        拿到幂等的 ``done``，§4.2），但 body 密钥没有再存在的理由。
        ``k_mac`` **必须留着**——幂等那条路要靠它验签。
        """
        self.k_body = None
        self.provider = None

    def drop_keys(self) -> None:
        """会话被作废时调用：两把钥匙都摘掉。"""
        self.k_mac = None
        self.drop_body_key()

    def touch_ttl(self, ttl: float) -> None:
        self.expires_at = time.monotonic() + ttl


# ---------- 会话表 ----------


class UploadManager:
    """上传会话的注册表与状态机。

    依赖注入（``dest_dir`` / 三个回调）是为了让它能脱离 api.py 单测：
    ``test_upload.py`` 直接给它一个 tmp 目录和一个收集器就能跑完整条链。

    :param chunk_size:    服务端建议的片大小（明文），随 upload_ready 下发
    :param session_ttl:   会话有效期（秒），每收到合法分片顺延
    :param photo_max_size: photo 类型的大小上限
    :param dest_dir:      落盘目录，默认 ``~/Downloads/PhoneMic``
    """

    def __init__(
        self,
        *,
        chunk_size: int = UPLOAD_CHUNK_SIZE,
        session_ttl: float = SESSION_TTL,
        photo_max_size: int = PHOTO_MAX_SIZE,
        dest_dir: Optional[Path] = None,
        on_file_done: Optional[Callable[[str, str, int], None]] = None,
        on_photo_done: Optional[Callable[[bytes, Optional[str], int], None]] = None,
    ) -> None:
        self.chunk_size = int(chunk_size)
        self.session_ttl = float(session_ttl)
        self.photo_max_size = int(photo_max_size)
        self.dest_dir = Path(dest_dir) if dest_dir else None
        self.on_file_done = on_file_done
        self.on_photo_done = on_photo_done

        self._sessions: Dict[str, UploadSession] = {}
        self._by_conn: Dict[int, Set[str]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- 查询 ----

    def get(self, sid: str) -> Optional[UploadSession]:
        """取活着的会话；不存在或已过期返回 None。

        过期 ⇒ 调用方按「拿不出凭证」处理（401、不读 body、不碰会话），
        真正的清理交给 ``sweep_expired()``。
        """
        session = self._sessions.get(sid)
        if session is None or session.expired:
            return None
        return session

    def __len__(self) -> int:
        return len(self._sessions)

    def sessions_for_conn(self, conn_id: int) -> list:
        return list(self._by_conn.get(conn_id, ()))

    # ---- 建立 ----

    @staticmethod
    def validate_begin(ref: Any, size: Any, name: Any) -> Optional[str]:
        """校验 ``upload_begin`` 的参数，返回错误码（None = 通过）。

        错误码即 ``upload_error.code``（§5.1）。
        """
        if ref not in VALID_REFS:
            return "bad_ref"
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return "bad_args"
        if ref == "file" and name is not None and not isinstance(name, str):
            return "bad_args"
        return None

    async def begin(
        self,
        *,
        kind: str,
        name: Any,
        size: int,
        conn_id: int,
        websocket: Any,
        algorithm: str,
        liveness: Any = None,
    ) -> UploadSession:
        """建一条上传会话：生成 sid 与两把一次性钥匙，分配落盘名并建 .part。

        Args:
            kind:       'file'（落盘）或 'photo'（写剪贴板）
            name:       客户端声明的原文件名（photo 可省略）
            size:       明文总字节数
            conn_id:    会话所属连接（WS 一断，该连接名下会话全部作废）
            websocket:  该连接的 WS 对象（当前只用于日志与归属判定）
            algorithm:  本次 WS 协商出的算法（两种算法统一走前缀，见 §5.8）
            liveness:   该连接的应用层判活状态，PUT 命中时刷新（§6.5）

        Raises:
            ValueError: 参数不合法（调用方应先过 ``validate_begin`` 与大小上限）
        """
        if kind not in VALID_REFS:
            raise ValueError(f"unknown ref: {kind!r}")
        if kind == "photo" and size > self.photo_max_size:
            raise ValueError(f"photo too large: {size} > {self.photo_max_size}")

        sid = secrets.token_urlsafe(SID_BYTES)
        material = secrets.token_bytes(_KEY_MATERIAL_SIZE)
        k_mac, k_body = material[:_KEY_SIZE], material[_KEY_SIZE:]
        provider = create_provider(algorithm, k_body)
        sink = await asyncio.to_thread(self._make_sink, kind, name, size)

        session = UploadSession(
            sid=sid,
            kind=kind,
            name=sink.name if kind == "file" else (sink.name or ""),
            size=size,
            conn_id=conn_id,
            websocket=websocket,
            algorithm=algorithm,
            k_mac=k_mac,
            k_body=k_body,
            provider=provider,
            sink=sink,
            expires_at=time.monotonic() + self.session_ttl,
            liveness=liveness,
        )
        self._sessions[sid] = session
        self._by_conn.setdefault(conn_id, set()).add(sid)
        self._loop = asyncio.get_running_loop()
        logger.info(
            "上传会话建立: sid=%s kind=%s size=%d saved=%r",
            sid, kind, size, session.saved,
        )
        return session

    def _make_sink(self, kind: str, name: Any, size: int):
        """构造 sink（同步；``begin`` 用 to_thread 调它，建 .part 是磁盘操作）。"""
        if kind == "file":
            return FileSink(name, dest_dir=self.dest_dir, on_done=self.on_file_done)
        cleaned = sanitize_name(name) if name not in (None, "") else None
        return PhotoSink(size, name=cleaned, on_done=self.on_photo_done)

    # ---- 验签（读 body 之前的门禁） ----

    @staticmethod
    def verify(session: UploadSession, offset: int, length: int, mac_text: Optional[str]) -> bool:
        """校验 ``X-Pm-Mac``；恒定时间比较，避免按字节比较泄漏信息。

        签名覆盖 ``"PUT\\n<sid>\\n<offset>\\n<len>"``——三项缺一不可，否则一份合法
        签名可以配上改过的偏移去覆盖文件的别的位置（§5.3）。
        """
        if session.k_mac is None:
            return False
        provided = decode_mac(mac_text)
        if provided is None:
            return False
        expected = upload_mac(session.k_mac, session.sid, offset, length)
        return hmac.compare_digest(expected, provided)

    # ---- 分片状态机（offset 三行判定） ----

    def judge(self, session: UploadSession, offset: int, length: int) -> ChunkVerdict:
        """只做判定，不读 body、不解密（§5.5 的那张三行表）。

        调用方保证此刻**验签已经通过**——所以「不作废」的那些分支是安全的，
        而不安全和不确定的一律 ``abort=True``。
        """
        recv = session.received

        if offset < 0 or length < 0:
            return ChunkVerdict("abort", 400, recv, abort=True, reason="negative offset/len")

        # 已收尾的会话：只服务「重发最后一片 → 拿幂等的 done」这一条路，
        # 绝不接受任何新写入（body 密钥此刻已经摘掉了）
        if session.done:
            return ChunkVerdict("ignore", 200, recv, done=True, saved=session.saved)

        if length > self.chunk_size:
            return ChunkVerdict("abort", 413, recv, abort=True,
                                reason=f"chunk {length} > {self.chunk_size}")
        if offset + length > session.size:
            return ChunkVerdict("abort", 413, recv, abort=True,
                                reason=f"offset+len {offset + length} > size {session.size}")

        if offset == recv:
            return ChunkVerdict("write", 200, recv)     # 正常推进（解密写盘在 commit_chunk）

        if offset + length <= recv:
            # 重复片（重放）：忽略，回 200，**不作废会话**
            logger.info("上传重复片，忽略: sid=%s offset=%d len=%d received=%d",
                        session.sid, offset, length, recv)
            return ChunkVerdict("ignore", 200, recv)

        # 其余（offset > received、部分重叠）：错位，自家客户端发不出来
        return ChunkVerdict("abort", 409, recv, abort=True,
                            reason=f"offset {offset} not writable (received={recv})")

    async def commit_chunk(
        self, session: UploadSession, offset: int, length: int, ciphertext: bytes,
    ) -> ChunkVerdict:
        """把一整片密文解密并写进 sink；收齐就顺手收尾。

        调用方保证 ``judge()`` 已判为「正常推进」且 body 已整片收齐——
        密文的认证标签在尾部，**必须整片**才能判断前面那段有没有被动过。
        """
        async with session.lock:
            if session.abort_event.is_set():
                # 会话已被取消，且不再回退——客户端早就 abort 了，拿不到响应也无所谓
                return ChunkVerdict("abort", 401, session.received, abort=True, reason="aborted")

            try:
                data = await asyncio.to_thread(self._decrypt_and_write, session, length, ciphertext)
            except CryptoError as e:
                # 密文解不开 ⇒ 这片不属于本会话，或片序号接不上。会话已不可用。
                # ⚠️ 回 409 而不是 401：401 那一档有一条硬约束「绝不碰会话」，
                # 而这里**必须**作废（§5.8 前提 3：写盘失败 / 解密失败即作废，
                # 不能靠回退 received 重来）。
                logger.warning("上传片解密失败，作废会话: sid=%s offset=%d - %s",
                               session.sid, offset, e)
                return ChunkVerdict("abort", 409, session.received, abort=True,
                                    reason=f"decrypt: {e}")
            except Exception as e:
                # 写盘失败（OSError / 磁盘满 / sink 状态错）同样作废，且**不能**回退
                # received —— 那会让 provider 的 _rx_seq 与 received 永久错位。
                logger.warning("上传片落盘失败，作废会话: sid=%s offset=%d - %s",
                               session.sid, offset, e)
                return ChunkVerdict("abort", 500, session.received, abort=True,
                                    reason=f"write: {e}")

            if data != length:
                logger.warning("上传片长度不符: sid=%s 声明 %d 实际 %d",
                               session.sid, length, data)
                return ChunkVerdict("abort", 409, session.received, abort=True,
                                    reason="len mismatch")

            session.received += length
            session.touch_ttl(self.session_ttl)
            if session.liveness is not None:
                # HTTP 活动也是「对端还活着」的证据（§6.5）
                session.liveness.touch()

            if session.received == session.size:
                return await self._finish(session)
            return ChunkVerdict("write", 200, session.received)

    def _decrypt_and_write(self, session: UploadSession, length: int, ciphertext: bytes) -> int:
        """线程内执行：解密 → 写盘。返回写入的明文字节数。

        ⚠️ **不要在这里剥片序号**：8 字节大端序号是 provider 的内部细节，
        ``decrypt()`` 返回的已经是应用明文（WS 那条路拿它直接喂 frame 解码，
        同一条约定）。外面再剥一次就会把正文的头 8 字节吃掉——而且只在
        「片 ≥ 9 字节」时才表现为内容错位，很不容易发现。
        """
        if session.provider is None:
            raise CryptoError("upload provider already released")
        data = session.provider.decrypt(ciphertext)
        if len(data) != length:
            raise CryptoError(f"plaintext {len(data)} != declared {length}")
        session.sink.write(data)
        return len(data)

    async def _finish(self, session: UploadSession) -> ChunkVerdict:
        """收齐：sink 收尾（改名落盘 / 交给剪贴板），然后摘掉 body 密钥。"""
        await asyncio.to_thread(session.sink.finish)
        session.done = True
        session.drop_body_key()          # k_mac 留着，幂等 done 那条路要靠它验签
        session.result = {"done": True, "saved": session.saved}
        logger.info("上传完成: sid=%s saved=%r size=%d", session.sid, session.saved, session.size)
        return ChunkVerdict("write", 200, session.received, done=True, saved=session.saved)

    # ---- 作废：唯一入口 ----

    async def abort_session(self, sid: str, reason: str = "") -> bool:
        """作废一条会话：关 sink、删 .part、摘掉会话与密钥、置 abort 标记。

        **幂等**：取不到会话直接返回 False。这是唯一的删除入口——用户在
        「片与片的间隙」点取消时根本没有在途 PUT，靠 PUT 收尾去删是永远等不到的
        （§5.6.3 约束 2）。
        """
        session = self._sessions.get(sid)
        if session is None:
            return False
        async with session.lock:
            if self._sessions.pop(sid, None) is None:
                return False                 # 已被别的路径作废
            self._unindex(session)
            session.abort_event.set()
            session.drop_keys()
            await asyncio.to_thread(session.sink.abort)
        logger.info("上传会话作废: sid=%s reason=%s", sid, reason or "-")
        return True

    async def abort_for_conn(self, conn_id: int, reason: str = "ws_closed") -> int:
        """作废某条 WS 连接名下的所有会话（§5.6.4：WS 一断即全取消）。

        ⚠️ 只能作用于**本连接**名下的会话：一个连接只服务一台手机（§5.6.1）。
        """
        sids = list(self._by_conn.get(conn_id, ()))
        count = 0
        for sid in sids:
            if await self.abort_session(sid, reason):
                count += 1
        if count:
            logger.info("连接 %s 名下 %d 条上传会话已作废（%s）", conn_id, count, reason)
        return count

    async def sweep_expired(self) -> int:
        """TTL 扫描：作废所有已过期的会话。返回作废条数。"""
        now = time.monotonic()
        stale = [s.sid for s in self._sessions.values() if s.expires_at <= now]
        for sid in stale:
            await self.abort_session(sid, "expired")
        return len(stale)

    async def abort_all(self, reason: str = "shutdown") -> int:
        """作废全部会话（测试 / 彻底复位用）。返回作废条数。"""
        count = 0
        for sid in list(self._sessions.keys()):
            if await self.abort_session(sid, reason):
                count += 1
        return count

    def close(self) -> None:
        """跨线程清理（``stop_server`` 用）：把作废调度回服务线程的事件循环。

        与旧的 ``TransferQueue.close()`` 同形——密钥**立刻**从会话记录里摘掉
        （不依赖那个调度是否跑得成），文件与句柄的清理交给 ``abort_session``。
        """
        sessions = list(self._sessions.values())
        for s in sessions:
            s.drop_keys()
        loop = self._loop
        if loop is None or loop.is_closed():
            self._sessions.clear()
            self._by_conn.clear()
            return
        for s in sessions:
            sid = s.sid
            try:
                loop.call_soon_threadsafe(
                    lambda sid=sid: asyncio.ensure_future(self.abort_session(sid, "shutdown")))
            except RuntimeError:
                pass

    # ---- 内部 ----

    def _unindex(self, session: UploadSession) -> None:
        sids = self._by_conn.get(session.conn_id)
        if not sids:
            return
        sids.discard(session.sid)
        if not sids:
            self._by_conn.pop(session.conn_id, None)
