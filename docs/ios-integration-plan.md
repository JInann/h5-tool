# h5-tool 集成 iOS WebView 调试（基于 pymobiledevice3）方案与开发计划

> 状态：**已上线 0.3.0**（2026-09-03 P5 完成：iOS 桥从 inspect-webkit 切换到 pymobiledevice3；真机 iPhone 11 iOS 26.5 全链路验证通过：懒启动 → 目标列表 → /cdp-ws 代理 Runtime.evaluate 1+1=2 → 真机页面执行）。
> 范围：一期只做 **iOS Web 调试**（目标列表 + 内嵌完整 DevTools），不做 iOS 截图/点击/镜像（iOS 协议限制，见「能力边界」）。
> 原则：**Android 与 iOS 共用同一套 API（/api/webview-targets），只在返回结果里区分平台；devtools.html 一套 UI 同时兼容两种设备。**

---

## 1. 背景

iOS Safari / App 内 WKWebView 没有原生 CDP，官方只有 WebKit 私有调试协议（WIR）。`pymobiledevice3`（doronz88 出品，2026-09 最新 11.3.x）通过 `webinspector cdp` 子命令在本地起 CDP server（FastAPI/uvicorn），把 iPhone/iPad 的 Safari 与 WKWebView 翻译成标准 CDP 端点（`/json/version` + `/json/list` + `/devtools/page/*` ws），与 h5-tool 现有的 Android 调试链路（`/json` → `/cdp-ws` 代理 → devtools-frontend）**协议形态完全一致**，天然可复用整套 UI 与代理。

集成方式：server.py 对每台真机 spawn 一个 `pymobiledevice3 webinspector cdp --port 9322+slot --udid <udid>` 子进程，作为 iOS 上游。
**前置条件**：本机已装 `pymobiledevice3`（`brew install pymobiledevice3` 或 `pipx install pymobiledevice3`），依赖 usbmuxd（Windows 上需 Apple Devices/iTunes）；**仅支持真机，不支持 iOS 模拟器**。

## 2. 现状盘点（可复用资产）

| 资产 | 位置 | 用途 |
|---|---|---|
| CDP 目标列表 UI + 5s 轮询 + 选中态 | `web/devtools.html` | iOS 目标直接复用渲染逻辑 |
| 完整 DevTools 前端 | `devtools-local/front_end`（`devtoolsPanelUrl()` 决策） | iframe 内嵌，iOS 同样支持 Elements/Console/Sources/Network/Storage |
| WebSocket 双向代理（无 Origin 握手） | `server.py` `handle_cdp_proxy()` / `/cdp-ws/` | 上游地址从「adb forward 端口」换成「iOS 桥端口」即可 |
| 目标字段精简 | `server.py` `_pick_targets()` | iOS targets 复用 |
| 子进程生命周期 + atexit 清理先例 | `scr_stream.py` + `stop_all` | 桥进程管理照抄 |
| 缺依赖降级提示先例 | `bin/h5-tool.js` `checkAdb()` | 非 macOS / 缺 bun 时红字提示不阻断 |

## 3. 总体架构

```
浏览器 devtools.html（列表 + iframe DevTools）  ←完全复用
   │  HTTP: GET /api/webview-targets?device=ios      （统一接口，结果含平台分区）
   │  WS:   /cdp-ws/<targetId>?device=ios            （代理新增 iOS 路由分支）
   ▼
server.py :12787  ── 新增 ios_bridge.py（懒启动/停止/健康探测/atexit）
   │  spawn: bunx --yes inspect-webkit@0.0.5 --port 9322
   ▼
inspect-webkit 桥 :9322  （WIR → CDP 翻译，纯 Bun/TS）
   │  usbmuxd（真机：lockdown TLS 配对）/ webinspectord_sim（模拟器，免配对）
   ▼
iPhone / iOS 模拟器  （Safari · App 内 WKWebView，iOS 16.4+ 需 isInspectable=true）
```

Android 既有链路（adb forward 9222+slot → `/cdp-ws` → DevTools）**完全不动，零回归**。

## 4. 统一 API 设计（核心）

### 4.1 GET /api/devices —— 设备列表扩展

原返回 `{devices, default, adb}`。扩展：

```jsonc
{
  "devices": [
    { "serial": "R58M1234ABC", "state": "device", "model": "M2012K11AC", "product": "xaga", "platform": "android" },
    { "serial": "ios", "platform": "ios", "model": "iOS (inspect-webkit)" }   // 虚拟设备，新增
  ],
  "default": "R58M1234ABC",
  "adb": { "installed": true }
}
```

- iOS 虚拟设备**总是列出**（不要求 macOS/bun 已就绪），点它才触发懒启动，未就绪时目标区给出安装/开启指引文案。
- `platform` 字段为新增；Android 条目保持原字段，避免破坏主控台 `app.js`。

### 4.2 GET /api/webview-targets?device=<serial|ios> —— 目标接口唯一化

返回体**向后兼容**（保留 `device`/`phone`/`phone_error` 键，主控台 index.html 零回归），新增平台分区：

```jsonc
{
  "device": "ios",                    // android serial 或 "ios"
  "platform": "ios",                  // "android" | "ios"
  "phone": [],                        // 兼容旧字段：platform=android 时填充
  "phone_error": null,
  "ios": {                            // platform=ios 时的分区（android 时恒为 null）
    "running": true,                  // 桥进程状态
    "starting": false,
    "port": 9322,
    "targets": [
      {
        "id": "sim:14407:PID:14526:1",  // 实测：<scope>:<n>:PID:<pid>:<idx>；真机前缀待验证
        "src": "ios",
        "title": "页面标题",
        "url": "https://example.com",
        "type": "page",
        "ws": "ws://127.0.0.1:9322/devtools/page/sim:14407:PID:14526:1",
        "frontend": "devtools://devtools/bundled/inspector.html?ws=...",
        "app": "com.apple.mobilesafari", // 后端从 description "(<bundle>)" 正则解析（实测存在）
        "device_name": null              // /json/list 无此字段；真机多设备分组待 P0.5
      }
    ],
    "error": null
  }
}
```

- **懒启动**：`GET /api/webview-targets?device=ios` 若桥未运行 → 后台线程 `ensure_running()`（幂等），本次返回 `running:false, starting:true`；前端 1s 后自然轮询到就绪。列表本就在轮询（5s），无需额外按钮；`starting` 超时（>40s，含首次 bunx 下载）或失败时 `error` 给指引。
- 后端按 `device == "ios"` 路由到桥，其余值走现有 Android 逻辑（`serial` 透传不变）。

### 4.3 POST /api/ios/restart（可选，兜底）

桥进程僵死时手动重建。对齐 `cdp.command()`「连接失效自动重建一次」的先例：`GET` 发现桥健康检查失败时自动重启一次，仍失败才报 `error`。此接口仅作前端「重试」按钮用。

### 4.4 /cdp-ws/<targetId>?device=ios —— 代理路由扩展

`handle_cdp_proxy(browser_sock, target_id, serial=device)` 增加分支：

```python
# 现有（Android）：serial None → default_serial()；port = resolve_port(serial, CDP_PORT, serials)
# 新增（iOS）：serial == "ios" 时跳过 adb，port = ios_bridge.port()
```

- 不做 adb、不碰 `default_serial()`（无 Android 设备时 iOS 也能工作）。
- target id 无需改名：WS 路径已带 `device=ios`，代理按 device 路由，Android/iOS 的 id 天然隔离。

## 5. 后端改动

| 文件 | 内容 |
|---|---|
| `ios_bridge.py`（新增） | 环境探测（`sys.platform == "darwin"`；`bun` 定位：`which bun` → `~/.bun/bin/bun` → `H5TOOL_BUN` env 覆盖）；`ensure_running()` 幂等启动 `bunx --yes inspect-webkit@0.0.5 --port <port>`；健康探测 `GET :port/json/list`（2s 超时）；`targets()` 拉取并 `_pick_targets` 化（追加 `app`：从 `description` 正则 `\(([a-zA-Z0-9._-]+)\)` 解析 bundle id）；`stop()` + atexit 注册；日志走 `access_log`（`[ios]` 前缀） |
| `server.py` | `import ios_bridge`；`/api/devices` 追加 iOS 虚拟设备；`/api/webview-targets` 平台路由（`get_webview_targets` 加 ios 分支）；`/cdp-ws` 代理 src 分支；`atexit.register` 清理桥 |

端口常量：`IOS_BRIDGE_PORT = int(os.environ.get("H5TOOL_IOS_PORT", 9322))`。
选 9322 原因：Android CDP 槽位占 9222–9285（`CDP_SLOTS=64`），本机 Chrome 调试端口约定 9333，两头不撞。

## 6. 前端改动（web/devtools.html 单页双平台）

| 改动 | 说明 |
|---|---|
| 设备 tab 渲染 | `/api/devices` 按 `platform` 区分样式/文案：Android 显示机型+serial（现状）；iOS tab 显示「iOS Safari / App」 |
| 目标列表 | `renderList` 支持平台：iOS 区显示 bridge 状态（starting/error 指引文案：macOS + bun 安装、真机配对「信任此电脑」、设备开 Web 检查器、App 需 `isInspectable`）；P0 确认字段后按 `app`/`device_name` 分组 |
| 打开调试 | `devtoolsUrl()` 在 `device == "ios"` 时 ws 指向 `/cdp-ws/<id>?device=ios`（现有拼接逻辑只加一个参数） |
| 轮询 | 现有 5s 轮询不变，iOS tab 激活时同样轮询即可（懒启动天然被轮询触发） |
| 主控台 index.html | **不改**（沿用 `phone` 字段，零回归） |

## 7. 能力边界（iOS，WIR 协议限制，非桥 bug）

| 能力 | iOS | 说明 |
|---|---|---|
| Elements / Console / Sources 断点 / Network / Storage | ✅ | 复用现有 iframe DevTools；注意 `Network.getResponseBody`（看不到响应体）、无 `Page.captureScreenshot` |
| `Page.navigate` 发送链接 / JS 执行 | ✅ | 一期不单独做入口，仅通过 DevTools 内操作；二期可在主控台加 |
| 截图 / 录屏 / 点击注入 / 镜像 | ❌ | iOS 无公开裸流与原生 Input 域，独立课题，一期不做 |
| iOS 模拟器 | ✅ | 免配对免 TLS；**由用户自行启动（不做自动 boot），工具只负责探测已启动实例** |
| 桌面 Safari | ❌ | Apple 私有 entitlement，枚举不了（工具限制） |
| 平台 | macOS only | 桥依赖 usbmuxd / webinspectord_sim；非 darwin 显示降级文案 |

## 8. 开发计划

### P0 环境核验（已完成 2026-09-03）
结论（模拟器 + iPhone 17，iOS 26 模拟器）：
1. 环境：darwin arm64。bun 装 **1.3.14**（官方脚本 `curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.14"` → `~/.bun/bin`，PATH 已写入 `~/.zshrc`）。不用 brew：其 formula 只发最新 1.4（Rust 重写版，社区争议大）。
2. 桥进程：`bunx --yes inspect-webkit@0.0.5 --port 9322` 一次成功（首次下载包约 10s）。`/json/list` 空态返回 `[]`（桥正常、无目标时为空数组，与「桥没起来」区分靠 running 状态）。
3. 目标字段（模拟器 Safari 两个 tab，实测原样）：
   `{"id":"sim:14407:PID:14526:1","description":"sim:14407 (com.apple.mobilesafari)","title":"...","type":"page","url":"...","webSocketDebuggerUrl":"ws://127.0.0.1:9322/devtools/page/sim:14407:PID:14526:1","devtoolsFrontendUrl":"devtools://..."}`
   结论：✅ id 格式 `<scope>:<n>:PID:<pid>:<idx>`（scope=sim 模拟器，真机预计 dev/udid 前缀，待真机验证）；✅ **description 含 App bundle id** → 后端解析出 `app` 字段做分组（Safari vs App 内 WKWebView）；❌ 无 device_name —— 多真机时按 id 前缀/description 区分，实现时再定（记为 P0.5）。
4. `bun` 探测路径（ios_bridge 用，宽松化）：`which bun` → `~/.bun/bin/bun` → `H5TOOL_BUN` env 覆盖。
5. 模拟器**由使用者自行启动**，工具只探测已启动实例（本次即用户已 boot 的 iPhone 17，无需 h5-tool 管理模拟器）。

### P1 后端模块（2-3h）
- 新增 `ios_bridge.py`（探测 / ensure_running / targets / stop / atexit）。
- `server.py`：`/api/devices` 扩展、`/api/webview-targets` ios 分区、`/cdp-ws` iOS 路由。
- 验收：`curl /api/devices` 见 ios 虚拟设备 → `curl '/api/webview-targets?device=ios'`（首次 starting，随后 targets 出页面）；Python 直连 `/cdp-ws/<id>?device=ios` 完成 WS 握手并发 `Runtime.enable` 有响应。

### P2 前端（2h）
- `devtools.html` 设备 tab 双平台 + iOS 目标区（状态/指引/列表）+ `device=ios` 的 ws 拼接。
- 验收：模拟器页面在右侧 iframe DevTools 中可看 Elements、可下断点、可 `Runtime.evaluate`；无 iOS 可用时错误指引文案正确。

### P3 收尾（0.5-1h）
- `package.json`：`files` 加 `ios_bridge.py`、版本 bump `0.2.0`。
- `README.md` / `INSTALL.md`：iOS 章节（前置：macOS + bun、真机配对、Web 检查器开关、App `isInspectable`）。
- 自测路径记录到 README；commit + push。

### P4 可选增强（另立需求）
- 主控台 index.html「发送链接」支持 iOS（`/api/navigate` 加 platform 分支，走桥端口 `Page.navigate`）。
- 真机配对引导（无设备时给出 设置→Safari→高级→Web 检查器 / App isInspectable 文案）。
- iOS 多设备按 `device_name` 分组展示。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| inspect-webkit 0.0.x 不稳定 / 行为漂移 | 锁版本 `@0.0.5`；能力按其 README 已知边界裁剪（不做截图/录屏/输入） |
| 首次 `bunx` 下载包 5-10s、断网失败 | `starting` 态 + 40s 超时；失败给安装指引；长期可预缓存到 bun 全局 |
| 无 iPhone 真机 | 模拟器全程兜底（P0 起即用），真机仅差配对 + TLS |
| 桥进程僵死 | status 健康探测；GET 发现异常自动重启一次再报错 |
| target id 冲突 | WS 路径带 `device=ios`，代理按设备路由，天然隔离 |

## 10. 参考资料
- inspect-webkit npm：https://www.npmjs.com/package/inspect-webkit
- inspect-webkit GitHub：https://github.com/EvanBacon/inspect-webkit
- WebKit 官方远程调试说明：https://webkit.org/?p=10701
