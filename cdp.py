#!/usr/bin/env python3
"""
纯 Python 实现的最小 CDP（Chrome DevTools Protocol）客户端。

只依赖标准库：通过 ADB 端口转发把手机 WebView 的 devtools socket 暴露到
本机 127.0.0.1:9222，然后用裸 socket 完成 WebSocket 握手与收发，
用于 Page.navigate（导航）与 Runtime.evaluate（执行 JS）。

不使用 node / ws，也不需要 SSL（CDP 走 ws:// 明文）。
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CDP_PORT = 9222
CDP_SLOTS = 64           # 多设备时 CDP 端口槽位数量（9222 ~ 9222+63）
DEVICE_SLOT_SEED = "h5tool-device-slot"


def _run(cmd, timeout=10):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def run_adb(cmd, timeout=10, serial=None):
    """执行 adb 命令。serial 非空时指定设备（adb -s <serial>），否则走默认设备。"""
    if serial:
        return _run(f"adb -s {serial} {cmd}", timeout=timeout)
    return _run(f"adb {cmd}", timeout=timeout)


def adb_available():
    """检测 adb 是否安装且可执行（启动/状态接口用，本机一次 30ms 级开销）。

    返回 {"installed": bool, "path": str|None, "version": str|None, "error": str|None}
    installed=False 时 error 给出原因与安装指引要点。
    """
    exe = shutil.which("adb")
    if not exe:
        return {
            "installed": False, "path": None, "version": None,
            "error": "adb 不在 PATH 中（Android 平台工具未安装）。macOS: brew install android-platform-tools",
        }
    try:
        r = _run(f"adb version", timeout=5)
    except Exception as e:
        return {"installed": False, "path": exe, "version": None,
                "error": f"adb version 执行失败：{e}"}
    if r.returncode != 0:
        return {"installed": False, "path": exe, "version": None,
                "error": (r.stderr or r.stdout or "adb version 异常").strip()[:200]}
    first = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
    return {"installed": True, "path": exe, "version": first[:120], "error": None}


def device_slot(serial, used_slots=()):
    """按 serial 计算稳定端口槽位（0 ~ CDP_SLOTS-1）。

    用 hash 固定序列，避免 adb devices 顺序抖动导致端口漂移；
    used_slots 里已有槽位（其它在线设备占用的）会被线性探测跳过，保证无冲突。
    """
    digest = hashlib.md5((DEVICE_SLOT_SEED + serial).encode()).hexdigest()
    slot = int(digest[:4], 16) % CDP_SLOTS
    used = set(used_slots or ())
    while slot in used:
        slot = (slot + 1) % CDP_SLOTS
    return slot


def cdp_port_for(serial, used_slots=()):
    """该设备对应的 CDP 转发端口（9222 + 槽位）。"""
    return CDP_PORT + device_slot(serial, used_slots=used_slots)


def resolve_port(serial, base, serials):
    """给 serial 分配一个端口（base + 槽位），避开其它在线设备已占用的槽位。

    serials: 当前所有在线设备的 serial 列表（含自己）。
    返回端口号。冲突时线性探测顺延，保证同机多设备端口不重叠。
    """
    others = [s for s in serials if s != serial]
    used = {device_slot(s) for s in others}
    return base + device_slot(serial, used_slots=used)


def list_devices():
    """解析 `adb devices -l`，返回 [{serial, model, product, state}]（按连接顺序）。"""
    r = run_adb("devices -l")
    out = []
    for line in r.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = {"serial": serial, "state": state, "model": None, "product": None}
        for kv in parts[2:]:
            if ":" in kv:
                k, v = kv.split(":", 1)
                if k in ("model", "product"):
                    info[k] = v
        out.append(info)
    return out


def default_serial():
    """返回默认设备 serial（adb devices 第一个处于 device 状态的），没有则 None。"""
    for d in list_devices():
        if d["state"] == "device":
            return d["serial"]
    return None


# WebView socket 名缓存：WiFi adb 下 cat /proc/net/unix 走网络很慢（2s+），
# 而 socket 名在 WebView 存活期间基本不变，缓存可让 status 轮询秒回。
# 缓存的是【已确认有可调试页面】的那个 socket（多 App 共存时挑对的那个）；
# 找不到不缓存，避免误判。
_sock_cache = {}          # serial -> (sock_name, ts)
_all_cache = {}           # serial -> (names, ts)
_SOCK_TTL = 8.0


def _all_webviews(serial, timeout=5, use_cache=True):
    """列出设备上【所有】WebView devtools socket 名（去重，保持 /proc/net/unix 顺序）。

    兼容两种命名（部分厂商浏览器会加前缀）：
      - @webview_devtools_remote_<pid>         标准 WebView
      - @browser_webview_devtools_remote_<pid> 小米浏览器等

    为什么是"所有"：同一台设备上可能同时存在多个 App 的 WebView socket
    （例如他ta星球 + 抖你 各持一个）。只取第一个会连到错误的 App——
    该 App 可能进程活着但没打开任何 H5 页面，/json 返回 []，表现为
    "CDP 未返回任何页面"。所以要把候选都拿出来逐个探测。
    """
    now = time.time()
    if use_cache:
        hit = _all_cache.get(serial)
        if hit and now - hit[1] < _SOCK_TTL:
            return list(hit[0])
    for _ in range(timeout):
        try:
            r = run_adb("shell cat /proc/net/unix", timeout=5, serial=serial)
        except Exception:
            # 设备/ADB 临时不可用（如超时），继续轮询
            time.sleep(0.5)
            continue
        names, seen = [], set()
        for line in r.stdout.split("\n"):
            if "webview_devtools_remote_" in line and "@" in line:
                name = line.split("@", 1)[-1].strip()
                # 形如 webview_devtools_remote_17241 或 browser_webview_devtools_remote_15802
                if name.split("_")[-1].isdigit() and name not in seen:
                    seen.add(name)
                    names.append(name)
        _all_cache[serial] = (names, now)
        return names
    return []


def _pkg_of_sock(serial, sock_name):
    """用 /proc/<pid>/cmdline 读该 socket 所属 App 的包名（兜底手段）。"""
    pid = sock_name.rsplit("_", 1)[-1]
    try:
        r = run_adb(f"shell cat /proc/{pid}/cmdline", timeout=3, serial=serial)
        return (r.stdout or "").replace("\x00", "").strip()
    except Exception:
        return ""


def _sock_label(serial, sock_name):
    """把 socket 名换成可读标签，用于报错定位：webview_devtools_remote_123(com.xxx.app)。"""
    pkg = _pkg_of_sock(serial, sock_name)
    return f"{sock_name}({pkg})" if pkg else sock_name


def find_webview(serial, timeout=5):
    """返回设备上第一个 WebView devtools socket 名（兼容旧调用）。

    注意：多 App 共存时这可能是"错误的那个"。建立 CDP 连接请走
    WebViewCDP.setup()——它会遍历所有候选并挑出真正有页面的那个。
    """
    now = time.time()
    hit = _sock_cache.get(serial)
    if hit and now - hit[1] < _SOCK_TTL:
        return hit[0]
    names = _all_webviews(serial, timeout=timeout)
    return names[0] if names else None


def _http_get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------- 多 App 支持：每个 WebView App 一个独立 CDP 转发端口 ----------
# 同一台设备上不同 App 的 WebView 各是一个 devtools socket，要同时调试就得各自
# forward 到不同的本地端口。端口用 (serial + socket 名) 哈希到固定槽位，保证同一
# App 每次拿到同一端口（DevTools 链接可复用），并线性探测避开其它设备与其它 App。
_app_ports = {}          # (serial, sock_name) -> port
_app_ports_lock = threading.Lock()


def app_cdp_port(serial, sock_name, serials=()):
    """为 (设备, App socket) 分配一个稳定且不冲突的 CDP 转发端口。

    稳定：同一 (serial, sock) 总是返回同一端口（进程存活期间）。
    不冲突：避开其它设备的基础槽位，也避开本进程内已分配给其它 App 的端口。
    """
    key = (serial, sock_name)
    with _app_ports_lock:
        got = _app_ports.get(key)
        if got:
            return got
        reserved = set(_app_ports.values())
        for s in serials:
            if s != serial:
                reserved.add(resolve_port(s, CDP_PORT, serials))
        slot = device_slot(f"{serial}#{sock_name}")
        for _ in range(CDP_SLOTS):
            cand = CDP_PORT + slot
            if cand not in reserved:
                _app_ports[key] = cand
                return cand
            slot = (slot + 1) % CDP_SLOTS
    raise CDPError(f"CDP 端口槽位耗尽（{CDP_SLOTS} 个已用满）")


def app_forward_port(serial, sock_name, serials=()):
    """确保 (设备, App) 的 adb forward 已建立，返回本地端口（幂等）。"""
    port = app_cdp_port(serial, sock_name, serials)
    run_adb(f"forward tcp:{port} localabstract:{sock_name}", serial=serial, timeout=5)
    return port


def _pkg_via_cdp(port, timeout=1.5):
    """从 CDP /json/version 读 App 包名（比 adb shell 读 cmdline 快，且不受 pid 复用影响）。"""
    try:
        v = _http_get_json(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return v.get("Android-Package") or ""
    except Exception:
        return ""


# 探测单个 App 的超时：够本地 adb 转发往返，又不会让"App 被后台冻结、socket 僵死"
# 的情况把整个列表拖住（此时请求会一直挂着直到超时）。
_APP_PROBE_TIMEOUT = 1.8


def _probe_app(serial, sock_name, serials):
    """探测单个 App：建 forward、拉目标列表、读包名。失败只记在该 App 的 error 里。"""
    item = {"socket": sock_name, "package": "", "port": None,
            "pages": [], "error": None}
    try:
        port = app_forward_port(serial, sock_name, serials)
    except Exception as e:
        item["error"] = f"端口分配失败：{e}"
        return item
    item["port"] = port
    try:
        pages = _http_get_json(f"http://127.0.0.1:{port}/json",
                               timeout=_APP_PROBE_TIMEOUT)
    except Exception as e:
        item["error"] = f"CDP 无响应（{e.__class__.__name__}）"
        # 兜底：socket 僵死时仍从进程 cmdline 拿到包名，前端至少能显示是哪个 App
        item["package"] = _pkg_of_sock(serial, sock_name)
        return item
    item["pages"] = pages or []
    item["package"] = _pkg_via_cdp(port) or _pkg_of_sock(serial, sock_name)
    return item


def list_webview_apps(serial, serials=None):
    """列出设备上**每个** WebView App 及其可调试页面（多 App 同时可见的关键）。

    返回 [{socket, package, port, pages, error}]，每个 App 一条：
      - pages：该 App 的原始 CDP /json 目标数组（未裁剪，交给调用方精简）
      - error：该 App 自己的探测错误（不影响其它 App）
    与 setup() 的区别：setup 只挑一个 App 建会话；本函数把全部 App 都摸一遍，
    供目标列表同时展示多个 App。

    各 App **并发**探测：某个 App 的 WebView 被系统冻结时 socket 会僵死，
    串行探测会让整个列表白等好几个超时周期。
    """
    if serials is None:
        serials = [d["serial"] for d in list_devices()]
    socks = _all_webviews(serial)
    if not socks:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(socks))) as ex:
        results = list(ex.map(lambda s: _probe_app(serial, s, serials), socks))
    # 把"第一个真的有页面的 App"记为默认：/api/status、不带 socket 的 eval/navigate
    # 都据此直连，不必再逐个探测一遍。
    for item in results:
        if item.get("pages"):
            _sock_cache[serial] = (item["socket"], time.time())
            break
    return results


class CDPError(Exception):
    pass


class WebSocketClient:
    """极简 WebSocket 客户端，仅支持 CDP 需要的文本帧收发。"""

    def __init__(self, ws_url, timeout=15):
        # ws_url 形如 ws://localhost:9222/devtools/page/XXXX
        assert ws_url.startswith("ws://")
        rest = ws_url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host or "localhost"
        self.port = int(port or "80")
        self.path = "/" + path
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        # 读取握手响应头（以 \r\n\r\n 结束）
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手失败：连接被关闭")
            data += chunk
        if b"101" not in data.split(b"\r\n", 1)[0]:
            raise CDPError(f"WebSocket 握手失败：{data.split(chr(13).encode())[0]!r}")

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CDPError("连接在读取过程中被关闭")
            buf += chunk
        return buf

    def recv_message(self):
        """读取一条完整消息（处理分片、忽略 ping/pong），返回文本。"""
        message = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise CDPError("服务端关闭了 WebSocket")
            if opcode == 0x9:  # ping -> 回 pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            # 0x1 text / 0x2 binary / 0x0 continuation
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def _send_control(self, opcode, payload=b""):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        header.append(0x80 | len(payload))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self):
        try:
            if self.sock:
                self._send_control(0x8)
                self.sock.close()
        except Exception:
            pass
        finally:
            self.sock = None


class CDPSession:
    """按设备管理 ADB 转发 + WebSocket，提供 navigate / evaluate。

    多设备支持：setup(serial) 为每台设备建立独立端口转发（9222+槽位），
    会话状态按 serial 缓存。
    """

    def __init__(self):
        self._sessions = {}   # key -> {"ws_url":..., "current_url":..., "socket":..., "port":...}

    @staticmethod
    def _key(serial, sock=None):
        """会话键：不指定 App 时用 serial（该设备的默认会话），指定时用 serial|socket。"""
        return f"{serial}|{sock}" if sock else serial

    def setup(self, serial=None, sock=None):
        """建立指定设备（默认第一台）到其 WebView 的 CDP 连接信息（幂等，可重复调用刷新）。

        sock 非空 → 只连这个 App 的 socket（多 App 并存时指定用哪个）。
        sock 为空 → 遍历设备上所有 App 的 WebView，挑第一个真正有页面的。
        返回 (serial, ws_url)。若传入的 serial 未连接，自动回退到默认设备。
        """
        if serial is None:
            serial = default_serial()
        if serial is None:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")

        serials = [d["serial"] for d in list_devices()]

        if sock:
            candidates = [sock]
        else:
            candidates = _all_webviews(serial, timeout=5)
            if not candidates:
                raise CDPError("未找到 WebView（请确保 App 已打开 H5 页面并开启了 WebView 调试）")
            # 上次确认有页面的那个 App 优先试（不看 TTL：只要它还在候选里就先试它，
            # 否则 /proc/net/unix 的顺序可能把"已僵死的 App"排到前面，白白等一个超时）。
            hit = _sock_cache.get(serial)
            if hit and hit[0] in candidates:
                candidates = [hit[0]] + [c for c in candidates if c != hit[0]]

        # 逐个候选 socket 探测：同一设备可能有多个 App 的 WebView，
        # 必须选到真的打开了页面的那个，否则报"未返回任何页面"。
        # 每个 App 用各自的转发端口，互不覆盖。
        tried = []
        for sock_name in candidates:
            try:
                port = app_forward_port(serial, sock_name, serials)
            except Exception as e:
                tried.append(f"{sock_name} → 端口分配失败: {e}")
                continue
            pages = None
            last_err = None
            # 先直接请求（forward 通常已就绪，省掉固定等待）；
            # 失败多半是 forward 刚建好还没生效，等一下再试一次。
            for attempt in range(2):
                if attempt:
                    time.sleep(0.3)
                try:
                    pages = _http_get_json(f"http://127.0.0.1:{port}/json",
                                           timeout=_APP_PROBE_TIMEOUT)
                    break
                except Exception as e:
                    last_err = e
            if pages is None:
                tried.append(f"{_sock_label(serial, sock_name)} → 读取失败: {last_err}")
                continue
            if not pages:
                tried.append(f"{_sock_label(serial, sock_name)} → 无打开的页面")
                continue

            # 优先选择类型为 page 的目标
            page_targets = [p for p in pages if p.get("type") == "page"] or pages
            target = page_targets[0]
            ws_url = target.get("webSocketDebuggerUrl")
            if not ws_url:
                tried.append(f"{_sock_label(serial, sock_name)} → 目标页面缺 webSocketDebuggerUrl")
                continue

            _sock_cache[serial] = (sock_name, time.time())
            sess = {
                "ws_url": ws_url,
                "current_url": target.get("url", ""),
                "socket": sock_name,
                "port": port,
            }
            self._sessions[self._key(serial, sock)] = sess
            # 首次连接时登记为该设备的默认会话（供 current_url / status 使用）
            if serial not in self._sessions:
                self._sessions[serial] = sess
            return serial, ws_url

        # 所有候选都失败：清掉"首选 App"记忆，下次重新按当前顺序挑
        _sock_cache.pop(serial, None)
        detail = "；".join(tried) if tried else "无候选"
        if len(candidates) == 1:
            raise CDPError(f"CDP 未返回任何页面（{detail}）")
        raise CDPError(
            f"CDP 未返回任何页面——设备上有 {len(candidates)} 个 WebView，"
            f"但都没打开页面：{detail}。请确认目标 App 的前台页面已加载 H5。"
        )

    def socket_of(self, serial=None, sock=None):
        """返回当前会话对应的 App socket 名（无则 None）。"""
        _, sess = self._session(serial, sock)
        return sess.get("socket")

    def _session(self, serial, sock=None):
        """取已建立的会话；未建立则先 setup。serial 为空用默认设备。"""
        if serial is None:
            serial = default_serial()
        if serial is None:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")
        key = self._key(serial, sock)
        if key not in self._sessions:
            self.setup(serial, sock=sock)
        return serial, self._sessions[key]

    def current_url(self, serial=None, sock=None):
        _, sess = self._session(serial, sock)
        return sess["current_url"]

    def _command(self, method, params=None, serial=None, sock=None):
        serial, sess = self._session(serial, sock)
        ws = WebSocketClient(sess["ws_url"])
        ws.connect()
        try:
            ws.send_text(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.time() + 15
            while time.time() < deadline:
                msg = json.loads(ws.recv_message())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise CDPError(msg["error"].get("message", "CDP error"))
                    return msg.get("result", {})
            raise CDPError("CDP 命令超时")
        finally:
            ws.close()

    def command(self, method, params=None, serial=None, sock=None):
        """执行命令，连接失效时自动重建一次。"""
        try:
            return self._command(method, params, serial=serial, sock=sock)
        except (OSError, CDPError):
            # WebView 可能已重建（PID 变化），刷新后重试一次
            self.setup(serial=serial, sock=sock)
            return self._command(method, params, serial=serial, sock=sock)

    def navigate(self, url, serial=None, sock=None):
        return self.command("Page.navigate", {"url": url}, serial=serial, sock=sock)

    def evaluate(self, expression, serial=None, sock=None):
        return self.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "allowUnsafeEvalBlocklistBypass": True,
        }, serial=serial, sock=sock)
