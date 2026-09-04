#!/usr/bin/env python3
"""
iOS Web 调试桥管理器（基于 pymobiledevice3 的 webinspector cdp，跨平台）。

pymobiledevice3（doronz88）是本机与 iOS 真机之间的瑞士军刀：`webinspector cdp`
子命令会起一个本地 CDP server（FastAPI/uvicorn），把 iPhone/iPad 上 Safari 与
App 内 WKWebView 的 WebKit 远程调试（WIR）翻译成标准 CDP 端点（/json/list +
/devtools/page/*），让 h5-tool 能用与 Android 相同的链路调试 iOS 真机。

与旧方案 inspect-webkit（bun 运行时、仅 macOS）的区别：
  - 纯 Python，pip / brew / pipx 均可安装，Windows / Linux / macOS 通用；
  - 每实例只连接一台设备（--udid），不支持 iOS 模拟器（真机 USB/RSD 专用）；
  - target 列表无 device/app 字段 → 设备分组改由 usbmux 设备表驱动。

本模块只负责一件事：管理这些桥进程（每台真机一个 pymobiledevice3 子进程）
的完整生命周期（懒启动 / 健康探测 / 端口分配 / 清理）。

约束：
  - 需要 pymobiledevice3 可执行文件（探测顺序见 find_pmd3）

运行时依赖探测（宽松，按序）：
  1. 环境变量 H5TOOL_PMD3（显式指定 pymobiledevice3 可执行文件路径）
  2. PATH 中的 pymobiledevice3
  3. PATH 中的 uvx（`uvx pymobiledevice3 ...` 兜底，首次会解析环境）
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

IOS_BRIDGE_PORT = int(os.environ.get("H5TOOL_IOS_PORT", "9322"))
ENGINE_NAME = "pymobiledevice3"

# 桥日志文件（进程退出/握手失败等排障信息），与 access_log 同目录
_DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".h5-tool", "logs")
LOG_DIR = os.environ.get("H5TOOL_LOG_DIR") or _DEFAULT_LOG_DIR
BRIDGE_LOG = os.path.join(LOG_DIR, "ios-bridge.log")

START_TIMEOUT = 60.0   # 实例就绪超时（pmd3 import 较重 + 首次连接设备）
HEALTH_TIMEOUT = 2.0   # 健康探测单次超时
USBMUX_TTL = 10.0      # usbmux 设备表缓存（秒）：8s 轮询下每 ~2 轮 spawn 一次 pmd3

# 稳定性加固（2026-09，iOS 26.5 真机 WIR 链路实测）：
LISTING_TTL = 3.0      # /json/list 结果缓存（秒）——pmd3 每次被查都会向设备上所有
                       #   app 发 forwardGetListing 触发 WIR 往返，5s 轮询若每次都真打，
                       #   页面/App 频繁切换时易把设备侧连接打 Reset（整接口 500）。
RETRY_COOLDOWN = 20.0  # 实例启动失败后的冷却（秒）——pmd3 撞 webinspectord 的
                       #   10s session 门禁时连续 spawn 只会次次失败（日志风暴）。
STALE_TARGETS_MAX = 15.0  # 最近成功目标列表的兜底有效期（秒）：超期后宁可空也不展示陈旧 target


class BridgeError(Exception):
    pass


def supported() -> bool:
    """pmd3 跨平台（win 需 Apple Devices / usbmuxd，失败会体现在 error）。"""
    return True


def find_pmd3():
    """按序探测 pymobiledevice3。返回 (argv_base, error)。

    argv_base 是可直接 spawn 的命令前缀列表（含 uvx 包装）。
    """
    env = os.environ.get("H5TOOL_PMD3")
    if env:
        return [env], None
    hit = shutil.which("pymobiledevice3")
    if hit:
        return [hit], None
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "pymobiledevice3"], None
    return None, ("未找到 pymobiledevice3 引擎。安装："
                  "brew install pymobiledevice3 或 pipx install pymobiledevice3"
                  "（Windows 另需 iTunes / Apple Devices 提供 usbmuxd 驱动；"
                  "或用 H5TOOL_PMD3 指定可执行文件路径）")


def _no_proxy_get_json(url, timeout=HEALTH_TIMEOUT):
    """GET 本机 JSON 端点。显式禁用代理 —— 本机服务直连即可，
    避免被环境 http_proxy 劫持（WorkBuddy 沙箱代理会转发并返回 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------------- usbmux 设备表 ----------------
_usbmux_lock = threading.Lock()
_usbmux_cache = None   # (ts, devices)


def _usbmux_devices():
    """本机 usbmuxd 上的 iOS 真机列表 [{udid, name}]（TTL 缓存，免频繁 spawn）。"""
    global _usbmux_cache
    now = time.time()
    with _usbmux_lock:
        if _usbmux_cache and now - _usbmux_cache[0] < USBMUX_TTL:
            return _usbmux_cache[1]
    base, _ = find_pmd3()
    if not base:
        return []
    try:
        out = subprocess.run(base + ["usbmux", "list"],
                             capture_output=True, text=True, timeout=8)
        raw = json.loads(out.stdout or "[]")
        devs = [{"udid": d.get("Identifier") or d.get("UniqueDeviceID"),
                 "name": d.get("DeviceName", "")}
                for d in raw if d.get("ConnectionType") == "USB"]
        devs = [d for d in devs if d["udid"]]
    except Exception:
        return []
    with _usbmux_lock:
        _usbmux_cache = (time.time(), devs)
    return devs


def _invalidate_usbmux():
    global _usbmux_cache
    with _usbmux_lock:
        _usbmux_cache = None


# ---------------- 单实例 ----------------
class _Instance:
    """一台真机对应的 pmd3 webinspector cdp 子进程。"""

    def __init__(self, udid, port):
        self.udid = udid
        self.port = port
        self.proc = None
        self.log_fh = None
        self.running = False
        self.starting = False
        self.error = None
        self.started_at = 0.0
        # 稳定性字段：
        self.next_retry_at = 0.0      # 启动失败冷却截止（time.time()）
        self.targets_error = None     # 最近一次 targets 查询错误（单实例）
        self._listing = None          # (ts, raw_pages) /json/list 成功结果缓存
        self.last_targets = None      # (ts, 精简目标列表) 查询失败时的兜底
        self._log_start_pos = 0       # 本次启动在共享日志中的起始偏移（失败诊断用）

    def _cmd(self, base):
        # 显式 --udid，多台真机互不串线
        return base + ["webinspector", "cdp",
                       "--port", str(self.port), "--udid", self.udid]

    def start(self, base):
        """spawn + 健康轮询（调用方持锁，本方法不做加锁）。成功置 running。"""
        self._open_log()
        try:
            self._precheck_port()
            cmd = self._cmd(base)
            self._log("launch: " + " ".join(cmd))
            self.proc = subprocess.Popen(
                cmd, stdout=self.log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,   # 独立进程组，stop 时整组清理
            )
            self._wait_healthy()
            if self.proc.poll() is not None:
                reason = self._failure_reason(self._tail_since_start())
                detail = ("：" + reason) if reason else ("，日志 %s" % BRIDGE_LOG)
                raise BridgeError("pymobiledevice3 进程已退出" + detail)
            self.running = True
            self.started_at = time.time()
            self.error = None
            self._log("instance ready %s on :%d" % (self.udid, self.port))
        except Exception as e:
            self._log("start failed %s: %s" % (self.udid, e))
            self._kill_proc()
            reason = self._failure_reason(self._tail_since_start())
            self._fail(reason or str(e))
        finally:
            self.starting = False

    def _precheck_port(self):
        """启动前探测端口空闲：被占用则直接报错（避免 spawn 后 bind 失败、
        health check 误打到占用方进程导致「假 running」）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError:
            raise BridgeError(
                "端口 %d 已被其它进程占用，请释放或用 H5TOOL_IOS_PORT 调整基端口"
                % self.port)
        finally:
            s.close()

    def _wait_healthy(self):
        """轮询本实例 /json/list 直到可响应（进程异常退出则立即失败）。"""
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                reason = self._failure_reason(self._tail_since_start())
                detail = ("：" + reason) if reason else ("，日志 %s" % BRIDGE_LOG)
                raise BridgeError("pymobiledevice3 提前退出" + detail)
            try:
                _no_proxy_get_json(
                    "http://127.0.0.1:%d/json/list" % self.port)
                return
            except Exception:
                time.sleep(0.8)
        raise BridgeError("实例启动超时（%ds），见 %s" % (START_TIMEOUT, BRIDGE_LOG))

    def stop(self):
        self.starting = False
        self.running = False
        self._kill_proc()
        if self.log_fh:
            try:
                self.log_fh.close()
            except Exception:
                pass
            self.log_fh = None

    def _kill_proc(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass

    def _open_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            # 记录本次启动前共享日志的大小：失败诊断只读自己启动后的增量，
            # 避免混入早前实例/其它设备的输出。
            self._log_start_pos = os.path.getsize(BRIDGE_LOG) if os.path.exists(BRIDGE_LOG) else 0
            self.log_fh = open(BRIDGE_LOG, "a", encoding="utf-8")
        except Exception:
            self.log_fh = None   # 打不开日志不阻塞启动

    def _log(self, line):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.log_fh:
                self.log_fh.write("%s %s\n" % (ts, line))
                self.log_fh.flush()
        except Exception:
            pass

    def _tail_since_start(self):
        """读取本次启动后 pmd3 追加到共享日志的增量内容（子进程输出）。"""
        try:
            with open(BRIDGE_LOG, "rb") as f:
                f.seek(self._log_start_pos)
                return f.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def _failure_reason(self, raw):
        """从 pmd3 输出里提取可直接给用户看的失败原因；识别不到返回 None。"""
        if not raw:
            return None
        if "WebInspectorNotEnabledError" in raw or "Web Inspector is disabled" in raw:
            return ("设备未开启 Web 检查器：iPhone 上到 设置→Safari→高级 打开「网页检查器」"
                    "（App 内 H5 需 App 开启 webView.isInspectable，一般 Debug 包已开启）")
        if "Authentication" in raw and ("fail" in raw.lower() or "error" in raw.lower()):
            return "设备未信任此电脑：解锁 iPhone 并在弹窗点「信任」，再试一次"
        if "No such service" in raw:
            return "设备未提供所需的调试服务（No such service），可能是系统限制或版本不匹配"
        return None

    def _fail(self, message):
        """记录启动失败并进入冷却（避免连续 spawn 撞 webinspectord session 门禁）。"""
        self.next_retry_at = time.time() + RETRY_COOLDOWN
        self.error = message


class IOSBridge:
    """pmd3 CDP 桥进程组管理（每台真机一个实例，模块级单例）。"""

    def __init__(self, base_port=IOS_BRIDGE_PORT):
        self.base_port = base_port
        self._lock = threading.Lock()
        self._instances = {}       # udid -> _Instance
        # 最近一次 targets() 聚合的查询状态（供 server 层区分「没页面」与「查询失败」）
        self._targets_error = None
        self._targets_stale = False

    # ---------------- 状态 ----------------
    def status(self):
        """轻量状态（不 spawn、不探测网络），供 /api/devices 等展示。"""
        tool, _ = find_pmd3()
        with self._lock:
            insts = list(self._instances.values())
        running = any(i.running for i in insts)
        starting = any(i.starting for i in insts)
        port = None
        for i in insts:                       # 首个 running / 首个分配端口
            if i.running:
                port = i.port
                break
        if port is None and insts:
            port = insts[0].port
        error = next((i.error for i in insts if i.error), None)
        return {
            "supported": supported(),
            "tool": bool(tool),
            "tool_name": ENGINE_NAME,
            "tool_cmd": " ".join(tool) if tool else None,
            "running": running,
            "starting": starting,
            "port": port,
            "error": error,
        }

    def is_running(self):
        with self._lock:
            return any(i.running for i in self._instances.values())

    def port_for(self, udid=None):
        """实例端口：udid 指定查对应实例；None 返回首个 running 实例端口。"""
        with self._lock:
            if udid:
                i = self._instances.get(udid)
                return i.port if i and i.running else None
            for i in self._instances.values():
                if i.running:
                    return i.port
        return None

    # ---------------- 生命周期 ----------------
    def ensure_started(self, udid=None):
        """幂等触发启动（后台线程执行，立即返回）。

        每次触发先扫 usbmux 设备表；无设备不 spawn（不产生无意义的
        zombie 进程），错误通过 status()/devices 空态表达。
        """
        devs = _usbmux_devices()
        if udid and not any(d["udid"] == udid for d in devs):
            return
        targets = [d["udid"] for d in devs]
        if udid:
            targets = [udid]
        if not targets:
            return
        with self._lock:
            # 端口按设备序从基端口顺延；已存在实例沿用旧端口（插拔不漂移）
            insts = []
            for idx, uid in enumerate(targets):
                inst = self._instances.get(uid)
                if inst is None:
                    inst = _Instance(uid, self.base_port + idx)
                    self._instances[uid] = inst
                insts.append(inst)
            for inst in insts:
                # 运行期进程已退出（崩溃/被杀/设备侧踢掉）而 running 未复位：
                # 先探测复位，允许按冷却策略重启，避免"状态卡 True、永远只出缓存列表"。
                if inst.proc is not None and inst.proc.poll() is not None:
                    inst.running = False
                    if inst.error is None:
                        inst.error = "桥进程已退出（可能被系统回收或连接被设备重置），将自动重启"
                if inst.running or inst.starting:
                    continue
                if time.time() < inst.next_retry_at:
                    # 启动失败冷却中：不再强拉（避免连续 spawn 撞 webinspectord
                    # 对连续 session 的 ~10s 门禁，形成"每次起来就挂"的日志风暴），
                    # 上次失败原因保留在 inst.error 供前端展示。
                    continue
                base, _ = find_pmd3()
                if not base:
                    inst.error = ("未找到 pymobiledevice3 引擎。安装："
                                  "brew install pymobiledevice3 或 "
                                  "pipx install pymobiledevice3"
                                  "（Windows 另需 iTunes / Apple Devices）")
                    continue
                inst.starting = True
                inst.error = None
                threading.Thread(target=self._start_worker,
                                 args=(inst, base), daemon=True).start()

    def _start_worker(self, inst, base):
        try:
            inst.start(base)
        except Exception as e:
            with self._lock:
                inst.error = str(e)
                inst.starting = False

    def stop_all(self):
        """停止全部实例（server.py 退出时 atexit 调用）。幂等。"""
        with self._lock:
            insts = list(self._instances.values())
            self._instances.clear()
        for inst in insts:
            inst.stop()
        _invalidate_usbmux()

    # ---------------- 目标列表 ----------------
    def targets(self, udid=None):
        """拉取实例 /json/list 并精简。全部未就绪返回 None。

        udid 指定 → 只查该实例；None → 聚合所有 running 实例（每个
        target 注入 device=<udid>，供前端按设备分组）。

        stale-while-error（2026-09 针对 iOS 26.5 真机 WIR 抖动加固）：
          - pmd3 的 /json/list 对设备上失联 app 零容错（asyncio.gather 不吞异常），
            只要有一个 app 的 WIR 连接被 Reset，整个接口就 500，页面列表会"凭空消失"；
          - 因此单实例查询失败时回退到最近一次成功结果（STALE_TARGETS_MAX 秒内），
            并把 error / stale 记到桥级状态（targets_state），供前端明确提示而非误导；
          - /json/list 结果按 LISTING_TTL 缓存：5s 轮询不必每次都向设备发起 WIR 往返，
            降低被打 Reset 的概率。
        """
        with self._lock:
            keys = [udid] if udid else list(self._instances)
            insts = [self._instances[u] for u in keys if u in self._instances]
        out, got = [], False
        errs, stale = [], False
        now = time.time()
        for inst in insts:
            if not inst.running:
                continue
            got = True
            try:
                if inst._listing and now - inst._listing[0] < LISTING_TTL:
                    pages = inst._listing[1]
                else:
                    pages = _no_proxy_get_json(
                        "http://127.0.0.1:%d/json/list" % inst.port, timeout=4.0)
                    inst._listing = (now, pages)
                inst.targets_error = None
                inst.last_targets = (now, self._simplify(inst, pages))
            except Exception as e:
                errs.append(str(e))
                inst.targets_error = "目标列表查询失败：" + str(e)[:160]
                # 查询失败 ≠ 没页面：回退最近一次成功结果，避免列表闪空误导
                if inst.last_targets and now - inst.last_targets[0] <= STALE_TARGETS_MAX:
                    out.extend(inst.last_targets[1])
                    stale = True
                continue
            out.extend(inst.last_targets[1])
        with self._lock:
            self._targets_error = (errs[0] or None)[:300] if errs else None
            self._targets_stale = stale
        return out if got else None

    @staticmethod
    def _simplify(inst, pages):
        return [{
            "id": p.get("id"),
            "device": inst.udid,       # 归属实例 udid（分组用）
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "type": p.get("type", "page"),
            "ws": p.get("webSocketDebuggerUrl"),
            "frontend": p.get("devtoolsFrontendUrl"),
            "app": None,               # pmd3 无 app 信息（WIR 层面缺）
        } for p in pages or []]

    def targets_state(self):
        """最近一次 targets() 的查询状态：error=失败原因 / stale=是否用了兜底列表。"""
        with self._lock:
            return {"error": self._targets_error, "stale": self._targets_stale}

    def devices(self):
        """usbmux 设备列表 [{key, scope, id}]（供 header 设备 tab）。

        与 target 归属的 udid 一致；实例未启动也可展示（点击即懒启动）。
        """
        devs = _usbmux_devices()
        return [{"key": d["udid"], "scope": "device", "id": d["udid"]}
                for d in devs]


# ============================================================
# 屏幕镜像 + 模拟点击（pmd3 `core-device display serve-web` 桥）
# ============================================================
# 能力说明（iOS 17+ / CoreDevice，跨平台 macOS / Windows / Linux）：
#   serve-web 在设备侧起 HEVC 媒体流，本地起 HTTP server：
#     - 浏览器打开 viewer（WebCodecs 解码，无需 ffmpeg / VNC）
#     - viewer 自带触摸控制（/touch）与 Home / 旋转 / 截屏 / 剪贴板
#       等端点 —— 即「镜像 + 点击滑动」一体，前端 iframe 嵌入即用。
#   serve-web 不带 --udid 选项，设备选择走 PYMOBILEDEVICE3_UDID 环境变量。
# 生命周期与上面的 webinspector cdp 桥完全独立（各自子进程 / 端口 / 隧道），
# 由调用方（server.py 的 /api/ios/mirror/*）按需启动与停止。
IOS_MIRROR_PORT = int(os.environ.get("H5TOOL_IOS_MIRROR_PORT", "12790"))
MIRROR_START_TIMEOUT = 90.0    # 冷启动（隧道 + 媒体流协商）慢于 cdp 桥
MIRROR_HEALTH_TIMEOUT = 2.0
MIRROR_LOG = os.path.join(LOG_DIR, "ios-mirror.log")
CAPABILITY_TIMEOUT = 30.0      # get-media-support-info 单次探测超时
CAPABILITY_TTL = 60.0          # 能力探测结果缓存（秒）


def _no_proxy_get_bytes(url, timeout=MIRROR_HEALTH_TIMEOUT):
    """GET 本机 URL 返回响应体（serve-web viewer 是 text/html，不能走 json 版）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return resp.read()


def _udid_rank(udid):
    """udid 在 usbmux 设备表中的槽位（mirror 端口按槽位顺延，与 cdp 桥一致）。"""
    devs = _usbmux_devices()
    for i, d in enumerate(devs):
        if d["udid"] == udid:
            return i
    return 0


class _MirrorInstance:
    """一台真机对应的 serve-web 子进程。"""

    def __init__(self, udid, port):
        self.udid = udid
        self.port = port
        self.url = "http://127.0.0.1:%d/" % port
        self.proc = None
        self.log_fh = None
        self.running = False
        self.starting = False
        self.error = None

    def _cmd(self, base):
        # 不带 --udid（core-device 依赖会与其冲突），设备选择用环境变量
        return base + ["developer", "core-device", "display", "serve-web",
                       "--bind", "127.0.0.1",
                       "--http-port", str(self.port), "--no-audio"]

    def start(self, base):
        """spawn + HTTP 健康轮询（调用方持锁）。成功置 running。"""
        self._open_log()
        try:
            self._precheck_port()
            cmd = self._cmd(base)
            env = os.environ.copy()
            env["PYMOBILEDEVICE3_UDID"] = self.udid
            self._log("launch: " + " ".join(cmd))
            self.proc = subprocess.Popen(
                cmd, stdout=self.log_fh, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
            self._wait_healthy()
            if self.proc.poll() is not None:
                raise BridgeError("serve-web 进程已退出，见 %s" % MIRROR_LOG)
            self.running = True
            self.error = None
            self._log("instance ready %s at %s" % (self.udid, self.url))
        except Exception as e:
            self._log("start failed %s: %s" % (self.udid, e))
            self._kill_proc()
            self.error = str(e)
        finally:
            self.starting = False

    def _precheck_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError:
            raise BridgeError(
                "镜像端口 %d 已被占用，请释放或用 H5TOOL_IOS_MIRROR_PORT 调整基端口"
                % self.port)
        finally:
            s.close()

    def _wait_healthy(self):
        """轮询 viewer 首页直到 200（进程提前退出则立即失败）。

        注意：HTTP 起来 ≠ 视频流已通 —— 设备未注册屏幕流服务时
        serve-web 仍能 serve 页面（eager 连接失败会 retry）。真正的能力
        可用性由 capability 探测（get-media-support-info）给出。
        """
        deadline = time.time() + MIRROR_START_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BridgeError("serve-web 提前退出，见 %s" % MIRROR_LOG)
            try:
                _no_proxy_get_bytes(self.url, timeout=MIRROR_HEALTH_TIMEOUT)
                return
            except Exception:
                time.sleep(0.8)
        raise BridgeError("镜像启动超时（%ds），见 %s" % (MIRROR_START_TIMEOUT, MIRROR_LOG))

    def stop(self):
        self.starting = False
        self.running = False
        self._kill_proc()
        if self.log_fh:
            try:
                self.log_fh.close()
            except Exception:
                pass
            self.log_fh = None

    def _kill_proc(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass

    def _open_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            self.log_fh = open(MIRROR_LOG, "a", encoding="utf-8")
        except Exception:
            self.log_fh = None

    def _log(self, line):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.log_fh:
                self.log_fh.write("%s %s\n" % (ts, line))
                self.log_fh.flush()
        except Exception:
            pass


class IOSMirror:
    """serve-web 镜像桥进程组管理（每台真机一个实例，模块级单例）。

    capability（设备是否注册屏幕流服务）单独缓存：
      - get-media-support-info 每次要 3~25s（起隧道 + 查询），不能同步卡接口；
      - probe 放后台线程，status 返回探测中/结果，前端据此给出提示。
    """

    def __init__(self, base_port=IOS_MIRROR_PORT):
        self.base_port = base_port
        self._lock = threading.Lock()
        self._instances = {}       # udid -> _MirrorInstance
        self._cap = {}             # udid -> (ts, capable: bool|None, hint: str|None)

    def _get_or_create(self, udid):
        with self._lock:
            inst = self._instances.get(udid)
            if inst is None:
                inst = _MirrorInstance(udid, self.base_port + _udid_rank(udid))
                self._instances[udid] = inst
            return inst

    # ---------------- 能力探测（后台、TTL 缓存） ----------------
    def _probe_worker(self, udid):
        base, err = find_pmd3()
        hint, capable = None, False
        if not base:
            hint = err or "未找到 pymobiledevice3 引擎"
        else:
            try:
                env = os.environ.copy()
                env["PYMOBILEDEVICE3_UDID"] = udid
                r = subprocess.run(
                    base + ["developer", "core-device", "display",
                            "get-media-support-info"],
                    capture_output=True, text=True, timeout=CAPABILITY_TIMEOUT,
                    env=env)
                # 注意：pmd3 CLI 失败时 returncode 仍为 0（错误打在 stderr 日志），
                # 必须用「stdout 是否是可解析的 JSON」判断成功。
                out_ok = (r.stdout or "").strip()
                if r.returncode == 0 and out_ok.startswith(("{", "[")):
                    capable = True
                else:
                    out = (r.stdout or "") + (r.stderr or "")
                    if "No such service" in out and "displayservice" in out:
                        # 实测（2026-09）：iOS 26.5 的 iPhone 11 / iPhone 14 Pro Max 均未注册
                        # displayservice —— 该服务随 iOS 27 DeviceHub 才开放给开发者工具，
                        # 与机型无关（换机无用）。iOS 26 及更早只能 Web 调试。
                        hint = ("屏幕流服务（displayservice）需 iOS 27+：当前系统（iOS 26 及更早）"
                                "不注册该服务，屏幕镜像不可用（Web 调试不受影响）。"
                                "iPhone 升级 iOS 27 后即可使用（日志 %s）" % MIRROR_LOG)
                    elif "not implemented" in out:
                        hint = ("设备未实现屏幕流服务接口（feature not implemented）："
                                "iOS 26 及更早系统未开放该能力，升级 iOS 27+ 后可用。"
                                "日志 %s" % MIRROR_LOG)
                    elif "Apple removed this service" in out:
                        hint = ("设备的 CoreDevice 未提供屏幕流服务（displayservice 缺失）："
                                "iOS 26 及更早的系统版本限制，与机型无关；升级 iOS 27+ 后可用"
                                "（Web 调试不受影响）。日志 %s" % MIRROR_LOG)
                    else:
                        lines = [ln for ln in out.splitlines() if ln.strip()]
                        tail = lines[-1].strip()[:200] if lines else "无输出"
                        if tail.startswith("http"):
                            tail = lines[-2].strip()[:200] if len(lines) > 1 else tail
                        hint = "屏幕流服务探测失败：" + tail
            except Exception as e:
                hint = "屏幕流服务探测异常：" + str(e)[:160]
        with self._lock:
            self._cap[udid] = (time.time(), capable, hint)

    def probe(self, udid, force=False):
        """按需触发能力探测（缓存 TTL 内不重复）。不阻塞。"""
        with self._lock:
            hit = self._cap.get(udid)
            if not force and hit and (time.time() - hit[0] < CAPABILITY_TTL):
                return
            self._cap[udid] = (time.time(), None, None)   # 标记探测中
        base, _ = find_pmd3()
        if not base:
            with self._lock:
                self._cap[udid] = (time.time(), False, "未找到 pymobiledevice3 引擎")
            return
        threading.Thread(target=self._probe_worker, args=(udid,),
                         daemon=True).start()

    def capability(self, udid):
        """返回 (capable: bool|None(未知/探测中), hint)。"""
        hit = self._cap.get(udid)
        if not hit or time.time() - hit[0] >= CAPABILITY_TTL:
            return None, None
        return hit[1], hit[2]

    # ---------------- 生命周期 ----------------
    def start(self, udid):
        """幂等触发启动（后台线程执行，立即返回）。"""
        devs = _usbmux_devices()
        if udid and not any(d["udid"] == udid for d in devs):
            return
        inst = self._get_or_create(udid)
        with self._lock:
            if inst.running or inst.starting:
                return
            base, err = find_pmd3()
            if not base:
                inst.error = err or "未找到 pymobiledevice3 引擎"
                return
            inst.starting = True
            inst.error = None
            threading.Thread(target=self._start_worker,
                             args=(inst, base), daemon=True).start()

    def _start_worker(self, inst, base):
        try:
            inst.start(base)
        except Exception as e:
            with self._lock:
                inst.error = str(e)
                inst.starting = False

    def stop(self, udid=None):
        """停止镜像：udid 指定停单台；None 停全部。幂等。"""
        with self._lock:
            if udid:
                inst = self._instances.pop(udid, None)
                targets = [inst] if inst else []
            else:
                insts = list(self._instances.values())
                self._instances.clear()
                targets = insts
        for inst in targets:
            inst.stop()

    def status(self, udid=None):
        """镜像状态（不 spawn）。udid 为空时聚合任意一台（首台 running 优先）。"""
        tool, _ = find_pmd3()
        with self._lock:
            insts = list(self._instances.values()) if udid is None \
                else [self._instances.get(udid)]
            insts = [i for i in insts if i]
        running, starting, error = False, False, None
        port = None
        for i in insts:                       # 首台 running / 首台启动中 / 首个端口
            if i.running:
                running, port = True, i.port
                error = None
                break
        if not running:
            for i in insts:
                if i.starting:
                    starting, port = True, (port or i.port)
                    break
        if not running and not starting and insts:
            inst = insts[0]
            port, error = inst.port, inst.error
        capable, cap_hint = self.capability(udid) if udid else (None, None)
        out = {
            "supported": supported(),
            "tool": bool(tool),
            "tool_cmd": " ".join(tool) if tool else None,
            "running": running,
            "starting": starting,
            "port": port,
            "url": ("http://127.0.0.1:%d/" % port) if port else None,
            "error": error,
            "capable": capable,        # True/False/None(未知或探测中)
            "capability_hint": cap_hint,
        }
        return out

    def is_running(self, udid=None):
        with self._lock:
            if udid:
                inst = self._instances.get(udid)
                return bool(inst and inst.running)
            return any(i.running for i in self._instances.values())


# 屏幕镜像模块级单例 + 便捷函数（server.py 使用）
mirror = IOSMirror()


def mirror_status(udid=None):
    return mirror.status(udid)


def mirror_start(udid):
    mirror.probe(udid)          # 顺带触发能力探测（TTL 缓存）
    mirror.start(udid)


def mirror_stop(udid=None):
    mirror.stop(udid)


def mirror_capability(udid):
    return mirror.capability(udid)


# 模块级单例 + server.py 可直接使用的函数
bridge = IOSBridge()


def status():
    return bridge.status()


def ensure_started(udid=None):
    bridge.ensure_started(udid)


def is_running():
    """是否有实例运行中（server.py 代理路由判断用）。"""
    return bridge.is_running()


def targets(udid=None):
    """拉取桥目标列表（全部未就绪返回 None）。"""
    return bridge.targets(udid)


def targets_state():
    """最近一次目标列表查询状态 {error, stale}（server.py 组装 iOS 分区时用）。"""
    return bridge.targets_state()


def devices():
    """拉取 usbmux 真机列表（无设备返回空列表）。"""
    return bridge.devices()


def port_for(udid=None):
    """实例端口查询（iOS 代理路由用）。"""
    return bridge.port_for(udid)


def stop_all():
    """停止全部实例（webinspector cdp 桥 + 屏幕镜像桥；server.py 退出时 atexit 调用）。"""
    bridge.stop_all()
    mirror.stop()
