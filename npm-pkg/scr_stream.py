#!/usr/bin/env python3
"""
scrcpy 视频流管理器（H.264 raw_stream 模式）。

只借 scrcpy 的设备端编码能力：把屏幕用 MediaCodec 硬件压成 H.264，
经 `adb forward` 转发到本机端口，本模块读取裸 H.264 字节流，
广播给所有订阅的 HTTP 客户端（供前端 WebCodecs 解码）。

刻意不实现 scrcpy 的握手/控制协议 —— 用 `raw_stream=true` 跳过封装，
直接拿到 Annex-B H.264；输入（点击/滑动）仍走原有 ADB 接口。

生命周期：
  - 首个客户端订阅时启动 scrcpy 会话；
  - 引用计数归零后宽限数秒自动停止（杀设备端 server、撤转发）。
"""
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path

import cdp  # 复用 run_adb

HERE = Path(__file__).parent
SCRCPY_SERVER_LOCAL = HERE / "scrcpy-server"
SCRCPY_SERVER_FALLBACK = Path("/opt/homebrew/Cellar/scrcpy/4.0/share/scrcpy/scrcpy-server")
DEVICE_SERVER = "/data/local/tmp/scrcpy-server.jar"
PORT = 27183
SCID_NAME = "scrcpy"          # localabstract 名（未指定 scid 时默认）
LAUNCH_TIMEOUT = 8.0
QUEUE_MAX = 30                # 每个订阅者缓冲帧数，超出丢弃最旧帧
GRACE = 3.0                   # 最后一个订阅者离开后保持运行的宽限秒数
END_SIGNAL = b""              # 广播中的结束信号


class StreamError(Exception):
    pass


class Streamer:
    def __init__(self):
        self._life_lock = threading.Lock()   # 控制 start/stop/running
        self._sub_lock = threading.Lock()    # 控制订阅者列表/引用计数
        self._proc = None
        self._sock = None
        self._reader = None
        self._subs = []
        self._refcount = 0
        self._running = False
        self._starting = False
        self._stop_ev = threading.Event()
        self._grace_timer = None
        self._server_path = self._resolve_server()
        # SPS/PPS 参数集缓存：scrcpy raw_stream 只在流开头发一次参数集，
        # 中途接入的订阅者拿不到就没法 WebCodecs configure。这里解析并缓存，
        # 新订阅者接入时先注入，保证随时能解码。
        self._param_sets = {}        # {nal_type: bytes} 含 start code 的完整 NAL
        self._nal_buf = bytearray()  # Annex-B 解析用的拼接缓冲

    def _resolve_server(self):
        if SCRCPY_SERVER_LOCAL.is_file():
            return str(SCRCPY_SERVER_LOCAL)
        if SCRCPY_SERVER_FALLBACK.is_file():
            return str(SCRCPY_SERVER_FALLBACK)
        raise StreamError("找不到 scrcpy-server，请确认已安装 scrcpy 或将其放入项目目录")

    # ---------------- 生命周期 ----------------
    def _push_server(self):
        cdp.run_adb(f"push {self._server_path} {DEVICE_SERVER}", timeout=30)

    def _adb_forward(self):
        cdp.run_adb(f"forward --remove tcp:{PORT}")
        cdp.run_adb(f"forward tcp:{PORT} localabstract:{SCID_NAME}")

    def _launch(self):
        # prepend-header-to-sync-frame: 每个关键帧(IDR)前都带上 SPS/PPS，
        # 中途接入的订阅者最多等一个 GOP 就能拿到参数集并开始解码。
        cmd = (
            f"shell CLASSPATH={DEVICE_SERVER} app_process / "
            f"com.genymobile.scrcpy.Server 4.0 "
            f"tunnel_forward=true audio=false control=false cleanup=false "
            f"raw_stream=true log_level=warn "
            f"video_codec_options=i-frame-interval=1,prepend-header-to-sync-frame=1"
        )
        cdp.run_adb("shell pkill -f com.genymobile.scrcpy.Server")
        time.sleep(0.3)
        self._proc = subprocess.Popen(
            f"adb {cmd}", shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _connect(self):
        # 设备端 server 启动需要 ~2s。若立即连接，adb forward 的本地监听会先 accept，
        # 但设备侧尚未 bind 抽象套接字，adb 随即关闭这条本地连接，导致读到空流。
        # 先等 server 起来，再带重试连接。
        time.sleep(2.0)
        deadline = time.time() + LAUNCH_TIMEOUT
        last_err = None
        while time.time() < deadline:
            try:
                self._sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)
                self._sock.settimeout(1.0)
                return
            except OSError as e:
                last_err = e
                time.sleep(0.5)
        raise StreamError(f"连接 scrcpy 视频端口超时：{last_err}")

    def _scan_param_sets(self, chunk):
        """从 Annex-B 字节流中解析 SPS(7)/PPS(8) 并缓存。

        scrcpy raw_stream 只在流开头发一次参数集，之后只有 P 帧/IDR。
        中途接入的订阅者拿不到 SPS/PPS 就无法 WebCodecs configure，
        所以这里把最新一次参数集缓存下来，subscribe 时注入给新订阅者。
        """
        self._nal_buf.extend(chunk)
        buf = self._nal_buf
        n = len(buf)
        i = 0
        while i + 3 < n:
            # 找 start code（3 或 4 字节）
            if buf[i] == 0 and buf[i + 1] == 0:
                if buf[i + 2] == 1:
                    sc_len = 3
                elif i + 3 < n and buf[i + 2] == 0 and buf[i + 3] == 1:
                    sc_len = 4
                else:
                    i += 1
                    continue
            else:
                i += 1
                continue
            start = i + sc_len
            # 找下一个 start code
            j = start
            while j + 3 < n:
                if buf[j] == 0 and buf[j + 1] == 0 and (
                        buf[j + 2] == 1 or
                        (j + 3 < n and buf[j + 2] == 0 and buf[j + 3] == 1)):
                    break
                j += 1
            if j + 3 >= n:
                # 不完整的尾部，留到下一轮
                self._nal_buf = bytearray(buf[i:])
                return
            nal = bytes(buf[start:j])
            if nal:
                t = nal[0] & 0x1f
                if t in (7, 8):  # SPS / PPS
                    self._param_sets[t] = bytes(buf[i:j])
            i = j
        self._nal_buf = bytearray()

    def _reader_loop(self):
        try:
            while not self._stop_ev.is_set():
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._scan_param_sets(chunk)
                self._broadcast(chunk)
        finally:
            self._running = False
            self._broadcast(END_SIGNAL)   # 通知所有订阅者流已结束

    def _broadcast(self, chunk):
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                if chunk:
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    q.put_nowait(chunk)
                else:
                    q.put_nowait(END_SIGNAL)
            except queue.Full:
                pass

    def ensure_started(self):
        with self._life_lock:
            if self._running or self._starting:
                return
            self._starting = True
        try:
            self._push_server()
            self._adb_forward()
            self._launch()
            self._connect()
            self._stop_ev.clear()
            self._running = True
            self._reader = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader.start()
        except Exception:
            self._running = False
            raise
        finally:
            with self._life_lock:
                self._starting = False

    def subscribe(self):
        with self._sub_lock:
            self._refcount += 1
            if self._grace_timer:
                self._grace_timer.cancel()
                self._grace_timer = None
        if not self.is_alive():
            self.ensure_started()
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self._sub_lock:
            self._subs.append(q)
            # 注入缓存的 SPS/PPS，让中途接入的订阅者也能初始化解码器
            for t in (7, 8):
                nal = self._param_sets.get(t)
                if nal:
                    try:
                        q.put_nowait(nal)
                    except queue.Full:
                        break
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)
            self._refcount = max(0, self._refcount - 1)
            if self._refcount == 0 and self._grace_timer is None:
                self._grace_timer = threading.Timer(GRACE, self.stop)
                self._grace_timer.start()

    def stop(self):
        with self._life_lock:
            if not self._running and self._proc is None:
                self._grace_timer = None
                return
            self._running = False
            self._stop_ev.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass
        cdp.run_adb("shell pkill -f com.genymobile.scrcpy.Server")
        cdp.run_adb(f"forward --remove tcp:{PORT}")
        with self._sub_lock:
            self._subs.clear()
            self._refcount = 0
            self._param_sets.clear()
            self._nal_buf = bytearray()
        with self._life_lock:
            self._proc = None
            self._sock = None
            self._grace_timer = None

    def is_alive(self):
        with self._life_lock:
            return self._running


# 模块级单例，供 server.py 直接引用
streamer = Streamer()
