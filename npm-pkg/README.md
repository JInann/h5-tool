# @fkjs/h5-tool

Android WebView H5 调试小工具后端 —— `npx` 一键启动（ADB + CDP + scrcpy，纯 Python 标准库，无第三方依赖）。

## 用法

```bash
# 启动后端（前台运行，Ctrl+C 停止；首次 npx 会提示安装，回车确认）
npx @fkjs/h5-tool start

# 换端口启动（如后端 12787 已被占用）
npx @fkjs/h5-tool start --port 12999

# 检测后端是否在运行
npx @fkjs/h5-tool status

# 停止（仅停本命令启动的实例；launchd 常驻的请用 launchctl 管理）
npx @fkjs/h5-tool stop
```

启动后浏览器打开 <http://127.0.0.1:12787/> 即可使用控制台（发送链接 / 截图 / 执行 JS / 实时镜像 / DevTools 调试）。

## 依赖

- Python 3（自动探测：`PYTHON` 环境变量 > `python3` > `python` > Windows `py -3`）
- `adb` 在 PATH 中，手机已连接并开启 USB 调试
- App 的 WebView 需开启调试（`WebView.setWebContentsDebuggingEnabled(true)`）
- 实时镜像走 scrcpy（包内已附带 `scrcpy-server`），浏览器需支持 WebCodecs（Chrome / Edge）

## 前后端分离

前端页面（`web/`）可部署到服务器静态托管，后端照常在各自电脑上 `npx @fkjs/h5-tool start` 启动：
浏览器打开服务器页面，页面里的 JS 跨域访问本机 `127.0.0.1:12787` 后端（`127.0.0.1` 指浏览器所在电脑，
每个使用者连自己电脑上的手机）。后端地址在 `web/config.js` 的 `H5TOOL_CONFIG.backend` 配置，
页面右上角「⚙ 后端」也可临时修改。Chrome 142+ 首次访问会弹「允许访问本地网络」授权框，点允许即可。

DevTools 调试面板资源默认走 `web/config.js` 的 `devtoolsPanel`（远程静态托管），无需本地构建。

## 说明

- 包体积 ~800KB（scrcpy-server 占大头），devtools-frontend（438MB）不入包，由远程面板替代。
- 源码仓库：GitHub `JInann/h5-tool`
