# H5 小工具

一个用来调试 Android App WebView 中 H5 页面的本地小工具。参考 `h5-verify` 技能的核心能力
（ADB + CDP 控制 WebView），做成一个**常驻的 Web 控制台**：浏览器打开即用，前端通过 HTTP
与宿主机上的 Python 后端通信。

## 能力

1. **发送 H5 链接到手机** — 让手机当前 WebView 导航到指定地址（CDP `Page.navigate`）。
2. **截图** — ADB 截取整屏，可下载 PNG。
3. **执行 JS 并获取返回值** — 在页面上下文执行任意 JS（CDP `Runtime.evaluate`，自动 await Promise）。
4. **实时屏幕镜像 + 模拟点击** — 支持 WebCodecs 的浏览器走 **scrcpy 硬件编码 + WebCodecs 解码**
   （30–60fps、低延迟，画到 `<canvas>`）；不支持时降级为 ADB 截图轮询（~3fps）。在画面上点击 =
   真机点击（`input tap`），拖动 = 滑动（`input swipe`），并提供「返回 / 主页」按键。

## 依赖

- `adb`（已在 PATH 中，且手机已连接、开启 USB 调试）
- App 的 WebView 需开启调试（`WebView.setWebContentsDebuggingEnabled(true)`）
- Python 3（无第三方依赖，纯标准库；CDP 的 WebSocket 是自己用 socket 实现的，不需要 node/ws）

## 使用

```bash
cd h5-tool
./run.sh                 # 默认 127.0.0.1:12787
# 或： python3 server.py --host 127.0.0.1 --port 12787
```

启动后在浏览器打开 <http://127.0.0.1:12787/> 即可。

## 架构

```
浏览器控制台 (web/)  ──HTTP──▶  server.py (127.0.0.1:12787)
                                   ├─ ADB: screencap / input tap|swipe|keyevent
                                   ├─ CDP (cdp.py, 纯 Python WebSocket)
                                   │     └─ adb forward tcp:9222 → webview_devtools_remote
                                   │          ├─ Page.navigate     (发送链接)
                                   │          └─ Runtime.evaluate  (执行 JS)
                                   └─ 视频流：scrcpy-server (设备端 MediaCodec 硬件 H.264)
                                         └─ adb forward tcp:27183 → 裸 H.264 字节流
                                              └─ /api/stream (chunked) → 浏览器 WebCodecs 解码到 <canvas>
```

## 依赖

- `adb`（已在 PATH 中，且手机已连接、开启 USB 调试）
- App 的 WebView 需开启调试（`WebView.setWebContentsDebuggingEnabled(true)`）
- Python 3（无第三方依赖，纯标准库；CDP 的 WebSocket 是自己用 socket 实现的）
- **scrcpy 视频流**：项目内已附带 `scrcpy-server`（与 scrcpy 4.0 配套）。实时镜像走 scrcpy
  硬件编码 + 浏览器 **WebCodecs** 解码。前端在支持 WebCodecs 的浏览器（Chrome / Edge）上使用
  scrcpy 管线（30–60fps、低延迟）；不支持时自动降级为原来的 PNG 轮询（约 3fps）。
- **输入（点击/滑动/按键）仍走原有 ADB**，不经过 scrcpy 控制通道。

## HTTP 接口

| 方法 | 路径              | 说明                                  |
|------|-------------------|---------------------------------------|
| GET  | `/api/status`     | 设备 / WebView / 分辨率 / scrcpy 状态 |
| GET  | `/api/screenshot` | 返回 PNG（静态截图 / 降级镜像用）      |
| GET  | `/api/stream`     | chunked H.264 裸流，供 WebCodecs 解码  |
| POST | `/api/navigate`   | `{url}` 导航当前 WebView              |
| POST | `/api/eval`       | `{expression}` 执行 JS，返回 `{value,type}` |
| POST | `/api/tap`        | `{x,y}` 设备坐标点击                  |
| POST | `/api/swipe`      | `{x1,y1,x2,y2,dur}` 滑动             |
| POST | `/api/key`        | `{code}` 按键（back=4, home=3）      |
| POST | `/api/text`       | `{text}` 输入文本                    |
| POST | `/api/restart`    | 重启本服务（launchctl kickstart，代码改动后生效） |

## 后台常驻 / 开机自启（macOS launchd）

已配置为 LaunchAgent：登录时自动启动，进程挂掉会自动拉起，日志写到
`h5-tool/h5-tool.log`。配置文件：`~/Library/LaunchAgents/com.aidog.h5-tool.plist`。

> 注意：`plist` 里显式补了 `PATH=/opt/homebrew/bin:...`，否则 launchd 的极简环境找不到 `adb`。

常用管理命令（`gui/$(id -u)` 是当前登录用户的域）：

```bash
# 启动 / 加载（首次或改完 plist 后）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aidog.h5-tool.plist

# 停止 / 卸载（改 plist 前先 bootout，再 bootstrap）
launchctl bootout gui/$(id -u)/com.aidog.h5-tool

# 立即重启
launchctl kickstart -k gui/$(id -u)/com.aidog.h5-tool

# 查看状态（state / pid / last exit code）
launchctl print gui/$(id -u)/com.aidog.h5-tool

# 看日志
tail -f h5-tool/h5-tool.log
```

改了 `server.py` / `cdp.py` / `web/` 后，代码会在下次重启后生效：
`launchctl kickstart -k gui/$(id -u)/com.aidog.h5-tool`。
改了 `plist` 本身则需要先 `bootout` 再 `bootstrap`。

## 说明

- 镜像画面上的坐标会按图片真实分辨率换算为设备坐标，因此点击位置准确。
- WebView 重建（PID 变化）时，CDP 会自动重连一次。
- 若右上角 WebView 指示灯为红色，把鼠标悬停在上面可看到具体原因。
