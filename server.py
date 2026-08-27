#!/usr/bin/env python3
"""
H5 小工具后端服务。

运行在宿主机上，H5 控制台通过 HTTP 与其通信（默认 127.0.0.1:12787）。
能力：
  1. 发送 H5 链接到手机当前 WebView（CDP Page.navigate）
  2. 截图（ADB screencap）
  3. 执行 JS 并返回结果（CDP Runtime.evaluate）
  4. 实时查看屏幕（前端按 3fps 轮询截图）+ 模拟点击（ADB input tap）

用法:
  python3 server.py                 # 默认 127.0.0.1:12787
  python3 server.py --host 0.0.0.0 --port 12787
"""

import argparse
import atexit
import json
import os
import queue
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cdp import CDPError, CDPSession, _http_get_json, run_adb, CDP_PORT, WebSocketClient
from scr_stream import StreamError, streamer

# 进程退出时清理设备端 scrcpy server 与转发
atexit.register(lambda: streamer.stop())

WEB_DIR = Path(__file__).parent / "web"

# devtools-frontend 静态资源目录（复用 Chrome DevTools UI）
# 优先取环境变量 DEVTOOLS_DIR，否则用项目下 devtools-local/front_end
DEVTOOLS_DIR = Path(os.environ.get("DEVTOOLS_DIR") or Path(__file__).parent / "devtools-local" / "front_end")

# 全局 CDP 会话（导航/执行 JS 复用）
cdp = CDPSession()

# 本服务自身监听的端口，禁止被「释放端口」误杀
SERVER_PORT = 12787

# 本服务在 macOS launchd 中以 LaunchAgent 形式常驻，重启走 launchctl kickstart
LAUNCHD_LABEL = "com.aidog.h5-tool"


def restart_service():
    """通过 launchctl 重启当前 LaunchAgent 服务。

    kickstart -k 会先 kill 旧进程、再由 launchd 拉起新进程（代码改动后生效）。
    这里延迟 0.3s 等 HTTP 响应先发出，再用独立 session 派发，避免自身被 kill 时
    连累 launchctl 命令。
    """
    try:
        time.sleep(0.3)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            check=True, start_new_session=True, capture_output=True, timeout=10,
        )
        sys.stderr.write("[h5-tool] 已请求 launchctl 重启服务\n")
    except Exception as e:
        sys.stderr.write(f"[h5-tool] 重启失败：{e}\n")


def adb_screencap_png(timeout=15):
    """用 exec-out 直接把 PNG 从 stdout 拉回，避免落盘与 CRLF 转换。"""
    r = subprocess.run(
        "adb exec-out screencap -p",
        shell=True, capture_output=True, timeout=timeout,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr.decode("utf-8", "replace") or "screencap 失败")
    return r.stdout


def adb_tap(x, y):
    r = run_adb(f"shell input tap {int(x)} {int(y)}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input tap 失败")


def adb_swipe(x1, y1, x2, y2, dur=200):
    r = run_adb(f"shell input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(dur)}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input swipe 失败")


def adb_keyevent(code):
    r = run_adb(f"shell input keyevent {int(code)}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input keyevent 失败")


def adb_text(text):
    # 空格需转义为 %s，其它特殊字符简单处理
    safe = text.replace(" ", "%s").replace("'", "")
    r = run_adb(f"shell input text '{safe}'")
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input text 失败")


def kill_port(port):
    """杀掉占用指定端口（TCP）的进程，返回被杀进程信息。"""
    r = subprocess.run(f"lsof -ti tcp:{int(port)}", shell=True,
                       capture_output=True, text=True, timeout=10)
    pids = [p for p in r.stdout.split() if p.strip().isdigit()]
    if not pids:
        return {"port": port, "killed": [], "message": f"端口 {port} 未被占用"}
    killed = []
    for pid in pids:
        name = subprocess.run(f"ps -p {pid} -o comm=", shell=True,
                              capture_output=True, text=True).stdout.strip()
        killed.append({"pid": int(pid), "name": name or "?"})
    subprocess.run(f"kill -9 {' '.join(pids)}", shell=True, timeout=10)
    return {"port": port, "killed": killed,
            "message": f"已释放端口 {port}，杀掉 {len(pids)} 个进程"}


def _ifconfig_inet(iface):
    """取指定网络接口的 IPv4 地址（inet），拿不到返回 None。"""
    try:
        out = subprocess.run(["ifconfig", iface], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _mac_wifi_ip():
    """macOS：从硬件端口列表里找到 Wi-Fi（或 AirPort）对应的设备（如 en0），取其 inet 地址。"""
    try:
        hw = subprocess.run(["networksetup", "-listallhardwareports"],
                            capture_output=True, text=True, timeout=5).stdout
        lines = hw.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"Wi-?Fi|AirPort", line):
                for nxt in lines[i + 1:i + 4]:
                    if "Device" in nxt:
                        return _ifconfig_inet(nxt.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _default_route_ip():
    """回退：取默认路由所在接口的 IPv4 地址（可能是以太网/VPN，不一定是 WiFi）。"""
    try:
        out = subprocess.run(["route", "get", "default"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"interface:\s*(\w+)", out)
        if m:
            return _ifconfig_inet(m.group(1))
    except Exception:
        pass
    return None


def _udp_trick_ip():
    """最后回退：用 UDP 连接外部地址探测出口网卡 IP（不真正发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def resolve_host_ip():
    """获取手机能访问到的本机局域网 IP（优先当前连接的 WiFi）。

    优先级：
      1. Wi-Fi 硬件端口的 IP —— 手机通常连的是这个 WiFi，最准确；
      2. 默认路由接口的 IP（以太网/VPN 等回退）；
      3. UDP 出口探测；
      4. 都不行则 127.0.0.1。
    跳过 127.0.0.1 这类回环地址。
    """
    for fn in (_mac_wifi_ip, _default_route_ip, _udp_trick_ip):
        ip = fn()
        if ip and ip != "127.0.0.1" and ip != "0.0.0.0":
            return ip
    return "127.0.0.1"


def get_screen_size():
    r = run_adb("shell wm size")
    # "Physical size: 1080x2400"  (可能还有 Override size)
    w = h = None
    for line in r.stdout.splitlines():
        if "size:" in line and "x" in line:
            try:
                dims = line.split(":")[1].strip()
                w, h = (int(v) for v in dims.split("x"))
            except Exception:
                pass
    return w, h


def get_status():
    r = run_adb("devices")
    devices = [l.split("\t")[0] for l in r.stdout.strip().splitlines()[1:]
               if l.strip() and "device" in l]
    status = {
        "device": devices[0] if devices else None,
        "device_connected": bool(devices),
        "webview": None,
        "current_url": None,
        "screen": None,
    }
    if devices:
        w, h = get_screen_size()
        if w:
            status["screen"] = {"width": w, "height": h}
        status["scrcpy"] = streamer._server_path is not None
        status["streaming"] = streamer.is_alive()
        try:
            cdp.setup()
            status["webview"] = True
            status["current_url"] = cdp.current_url
        except CDPError as e:
            status["webview"] = False
            status["webview_error"] = str(e)
    return status


def _pick_targets(pages):
    """精简 CDP /json 返回的 target 字段，避免把大对象全量透传。"""
    out = []
    for p in pages or []:
        out.append({
            "id": p.get("id"),
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "type": p.get("type", ""),
            "ws": p.get("webSocketDebuggerUrl"),
            "frontend": p.get("devtoolsFrontendUrl"),
        })
    return out


def get_webview_targets():
    """汇总可调试目标：手机 WebView（9222，经 adb forward）+ 本机 Chrome（9333）。

    手机源复用 cdp.setup()（幂等，自动建 adb forward 并刷新目标）；
    本机 Chrome 源直接读 9333（由 Chrome Debug.app 启动）。
    """
    result = {"phone": [], "phone_error": None, "chrome": [], "chrome_error": None}
    try:
        cdp.setup()
        result["phone"] = _pick_targets(
            _http_get_json(f"http://127.0.0.1:{CDP_PORT}/json"))
    except Exception as e:
        result["phone_error"] = str(e)
    try:
        result["chrome"] = _pick_targets(_http_get_json("http://127.0.0.1:9333/json"))
    except Exception as e:
        result["chrome_error"] = str(e)
    return result


# ---------- WebSocket 代理（CDP Origin 校验绕过） ----------
# Android WebView（Chrome 111+ 内核）的 CDP server 会校验 WebSocket 的 Origin：
# 浏览器 iframe（Origin=http://127.0.0.1:12787）直连 ws://127.0.0.1:9222 会被 403 拒绝。
# 本代理作为中间层：浏览器连 h5-tool 的 /cdp-ws/<targetId>，
# 代理用 cdp.WebSocketClient（不带 Origin）转发到 9222，实现双向透传。

import hashlib
import base64
import struct

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被关闭")
        buf += chunk
    return buf


def _read_ws_frame(sock):
    """读一帧，返回 (fin, opcode, payload)。payload 已解 mask。"""
    b0, b1 = _recv_exact(sock, 2)
    fin = b0 & 0x80
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def _write_ws_frame(sock, opcode, payload, mask_key=None, fin=True):
    """发一帧。mask_key 为 None 则不 mask（服务端→客户端方向）。"""
    header = bytearray([0x80 | opcode] if fin else [opcode])
    length = len(payload)
    if length < 126:
        header.append((0x80 if mask_key else 0) | length)
    elif length < 65536:
        header.append((0x80 if mask_key else 0) | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append((0x80 if mask_key else 0) | 127)
        header.extend(struct.pack(">Q", length))
    if mask_key:
        header.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + payload)


def _ws_pump(src, dst, mask_key, fin_flag):
    """把 src 的帧转发到 dst。返回 False 表示应结束（close 或异常）。"""
    while True:
        try:
            fin, opcode, payload = _read_ws_frame(src)
        except Exception:
            return False
        if opcode == 0x8:  # close：透传后结束
            try:
                _write_ws_frame(dst, 0x8, payload, mask_key=mask_key)
            except Exception:
                pass
            return False
        try:
            if opcode == 0x9:  # ping
                _write_ws_frame(dst, 0x9, payload, mask_key=mask_key)
            elif opcode == 0xA:  # pong
                _write_ws_frame(dst, 0xA, payload, mask_key=mask_key)
            else:  # text / binary / continuation
                _write_ws_frame(dst, opcode, payload, mask_key=mask_key,
                                fin=bool(fin))
        except Exception:
            return False


def handle_cdp_proxy(browser_sock, target_id):
    """浏览器 WebSocket <-> 手机 CDP 双向代理。

    browser_sock 已由 Handler 完成握手；target 侧复用 cdp.WebSocketClient
    （握手不带 Origin，Android WebView 的 9222 才能 101）。
    """
    upstream = WebSocketClient(f"ws://127.0.0.1:{CDP_PORT}/devtools/page/{target_id}")
    try:
        upstream.connect()
    except Exception as e:
        try:
            _write_ws_frame(browser_sock, 0x8, b"", fin=True)
        except Exception:
            pass
        sys.stderr.write(f"[h5-tool] CDP 代理上游连接失败 {target_id}: {e}\n")
        return
    upstream_sock = upstream.sock

    def close_both():
        for s in (browser_sock, upstream_sock):
            try:
                s.close()
            except Exception:
                pass

    def to_upstream():
        # 浏览器帧已 mask → 转发给 9222 需重新 mask
        if not _ws_pump(browser_sock, upstream_sock, os.urandom(4), True):
            close_both()

    def to_browser():
        # 9222 帧未 mask → 直接透传给浏览器
        if not _ws_pump(upstream_sock, browser_sock, None, True):
            close_both()

    t1 = threading.Thread(target=to_upstream, daemon=True)
    t2 = threading.Thread(target=to_browser, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def _claude_tcp_latency(host, port=443, timeout=5.0):
    """TCP 建连延迟（直连，绕过代理，看宿主机到目标的裸延迟）。"""
    t = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return (time.perf_counter() - t) * 1000.0


def _claude_https_latency(host, timeout=12.0):
    """HTTPS 请求往返延迟（urllib 自动走 HTTPS_PROXY 环境变量）。打到 /v1/models，
    预期 401，但能完整测出「经出口代理 → Anthropic」的真实往返。"""
    url = f"https://{host}/v1/models"
    t = time.perf_counter()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "h5-tool"})
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        pass
    return (time.perf_counter() - t) * 1000.0


def claude_latency_probe(rounds=4):
    """测当前出口节点到 api.anthropic.com 的延迟，返回结构化结果供前端展示。"""
    host = "api.anthropic.com"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "（直连）"
    # warmup：丢弃首轮（代理冷启动会虚高），只统计稳态
    try:
        _claude_tcp_latency(host)
        _claude_https_latency(host)
    except Exception:
        pass
    tcp, https = [], []
    for _ in range(rounds):
        try:
            tcp.append(_claude_tcp_latency(host))
        except Exception:
            pass
        try:
            https.append(_claude_https_latency(host))
        except Exception:
            pass

    def _stat(s):
        if not s:
            return None
        s = sorted(s)
        p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
        return {
            "min": round(min(s), 1),
            "p50": round(statistics.median(s), 1),
            "p90": round(p90, 1),
            "max": round(max(s), 1),
            "n": len(s),
        }

    return {
        "ok": bool(https),
        "proxy": proxy,
        "host": host,
        "tcp": _stat(tcp),
        "https": _stat(https),
        "ts": time.strftime("%H:%M:%S"),
    }


def simplify_eval(result):
    """把 Runtime.evaluate 的结果整理成 {value, type, error}。"""
    if result.get("exceptionDetails"):
        exc = result["exceptionDetails"]
        text = exc.get("exception", {}).get("description") or exc.get("text") or "执行出错"
        return {"error": text}
    obj = result.get("result", {})
    out = {"type": obj.get("type"), "subtype": obj.get("subtype")}
    if "value" in obj:
        out["value"] = obj["value"]
    elif obj.get("description") is not None:
        out["value"] = obj["description"]
    else:
        out["value"] = None
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def finish(self):
        # 视频流客户端中途断开时会触发 BrokenPipe，压制以避免刷日志
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, fmt, *args):
        # 精简日志：截图轮询太频繁，不打印
        if "screenshot" in self.path:
            return
        sys.stderr.write("[h5-tool] %s\n" % (fmt % args))

    # ---------- 工具方法 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, cache=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", f"max-age={cache}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _stream_video(self):
        """订阅 scrcpy 视频流，以 chunked 二进制形式转发给浏览器（WebCodecs 解码）。"""
        # 先发响应头，让浏览器立刻进入连接态；scrcpy 启动时数据稍后到达。
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            q = streamer.subscribe()
        except StreamError as e:
            try:
                self.wfile.write(("0\r\n\r\n").encode("latin-1"))
            except Exception:
                pass
            return
        try:
            while True:
                try:
                    chunk = q.get(timeout=1)
                except queue.Empty:
                    if not streamer.is_alive():
                        break
                    continue
                if chunk == b"":       # 流结束信号
                    break
                try:
                    self.wfile.write(("%X\r\n" % len(chunk)).encode("latin-1") + chunk + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, OSError):
                    break
        finally:
            streamer.unsubscribe(q)
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass

    def _serve_static(self, rel):
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ext = path.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._send_bytes(path.read_bytes(), ctype)

    def _serve_devtools(self, rel):
        """托管 devtools-frontend 静态资源（inspector.html 及其 js/css/资源）。

        保持原始目录结构按相对路径 serve；目录未构建时给出友好提示。
        """
        if not DEVTOOLS_DIR.is_dir():
            self._send_json({
                "error": "devtools 资源未构建",
                "hint": "设置 DEVTOOLS_DIR 指向 devtools-frontend 的 front_end 产物目录",
            }, 404)
            return
        # 目录请求：跳到 inspector.html（DevTools 入口）
        if not rel or rel.endswith("/"):
            rel = "inspector.html"
        path = (DEVTOOLS_DIR / rel).resolve()
        if not str(path).startswith(str(DEVTOOLS_DIR.resolve())) or not path.is_file():
            self._send_json({"error": "devtools: not found"}, 404)
            return
        ext = path.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".map": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".gif": "image/gif",
            ".jpg": "image/jpeg",
            ".ico": "image/x-icon",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
            ".wasm": "application/wasm",
        }.get(ext, "application/octet-stream")
        # devtools 资源较大且不变，浏览器缓存 1 小时
        self._send_bytes(path.read_bytes(), ctype, cache=3600)

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html")
            elif path == "/devtools.html":
                self._serve_static("devtools.html")
            elif path == "/api/status":
                self._send_json(get_status())
            elif path == "/api/webview-targets":
                self._send_json(get_webview_targets())
            elif path == "/api/screenshot":
                self._send_bytes(adb_screencap_png(), "image/png")
            elif path == "/api/stream":
                self._stream_video()
            elif path == "/api/claude-latency":
                self._send_json(claude_latency_probe())
            elif path.startswith("/cdp-ws/"):
                # WebSocket 代理：devtools 前端连本地代理，绕过 WebView CDP 的 Origin 校验
                if self.headers.get("Upgrade", "").lower() != "websocket":
                    return self._send_json({"error": "需要 WebSocket 连接"}, 400)
                target_id = path[len("/cdp-ws/"):]
                if not target_id or "/" in target_id or "?" in target_id:
                    return self._send_json({"error": "target 无效"}, 400)
                key = self.headers.get("Sec-WebSocket-Key", "")
                if not key:
                    return self._send_json({"error": "缺少 Sec-WebSocket-Key"}, 400)
                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n"
                    "\r\n"
                )
                self.connection.sendall(resp.encode())
                self.close_connection = True
                handle_cdp_proxy(self.connection, target_id)
            elif path.startswith("/devtools/"):
                self._serve_devtools(path[len("/devtools/"):])
            elif path.startswith("/"):
                self._serve_static(path.lstrip("/"))
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_body()
            if path == "/api/navigate":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._send_json({"error": "缺少 url"}, 400)
                # 将 {ip} 占位符替换为本机实际 LAN IP（如 http://{ip}:5173 → http://172.16.136.225:5173）
                host_ip = resolve_host_ip()
                resolved = url.replace("{ip}", host_ip)
                replaced_ip = resolved != url
                url = resolved
                if not url.startswith(("http://", "https://", "file://", "about:")):
                    url = "https://" + url
                cdp.navigate(url)
                self._send_json({"ok": True, "url": url,
                                 "ip": host_ip, "replaced_ip": replaced_ip})

            elif path == "/api/eval":
                expr = body.get("expression") or ""
                if not expr.strip():
                    return self._send_json({"error": "缺少 expression"}, 400)
                result = cdp.evaluate(expr)
                self._send_json({"ok": True, **simplify_eval(result)})

            elif path == "/api/tap":
                adb_tap(body["x"], body["y"])
                self._send_json({"ok": True})

            elif path == "/api/swipe":
                adb_swipe(body["x1"], body["y1"], body["x2"], body["y2"],
                          body.get("dur", 200))
                self._send_json({"ok": True})

            elif path == "/api/key":
                # 常用：back=4, home=3, enter=66, back-space=67
                adb_keyevent(body.get("code", 4))
                self._send_json({"ok": True})

            elif path == "/api/text":
                adb_text(body.get("text", ""))
                self._send_json({"ok": True})

            elif path == "/api/kill-port":
                try:
                    port = int(body.get("port"))
                except (TypeError, ValueError):
                    return self._send_json({"error": "端口无效"}, 400)
                if not (1 <= port <= 65535):
                    return self._send_json({"error": "端口需在 1-65535 之间"}, 400)
                if port == SERVER_PORT:
                    return self._send_json({"error": "不能释放本工具自身占用的端口"}, 400)
                self._send_json({"ok": True, **kill_port(port)})

            elif path == "/api/restart":
                # 异步重启：先回 200 让前端拿到响应，再触发 launchctl kickstart
                threading.Thread(target=restart_service, daemon=True).start()
                self._send_json({"ok": True, "message": "正在重启服务…"})

            else:
                self._send_json({"error": "not found"}, 404)
        except CDPError as e:
            self._send_json({"error": str(e)}, 502)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


def main():
    parser = argparse.ArgumentParser(description="H5 小工具后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12787)
    args = parser.parse_args()

    global SERVER_PORT
    SERVER_PORT = args.port

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"[h5-tool] 服务已启动：{url}")
    print("[h5-tool] 在浏览器打开上面的地址即可使用控制台。按 Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[h5-tool] 已退出")
        server.shutdown()


if __name__ == "__main__":
    main()
