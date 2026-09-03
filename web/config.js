// h5-tool 全局配置（前后端分离模式）
//
// 本地单机模式（直接打开 http://127.0.0.1:12787/）无需修改任何配置；
// 部署到服务器后，浏览器打开服务器页面，页面里的 JS 会请求下面的 backend 地址。
// 注意：127.0.0.1 指「浏览器所在的那台电脑」（即后端跑在那里），不是服务器。
window.H5TOOL_CONFIG = {
  // 后端地址：h5-tool Python 后端，跑在使用者自己电脑上（127.0.0.1:12787）
  backend: "http://127.0.0.1:12787",
  // DevTools 前端（inspector.html）入口。留空 = 自动判断（推荐，本机/部署版都正确）：
  //   - 页面在本机打开（host 为 127.0.0.1/localhost）→ 同源 /devtools/inspector.html（h5-tool 自带 front_end）
  //   - 页面在服务器部署版打开（如 CloudBase）→ 同源 /front_end/inspector.html（与 devtools.html 一起部署）
  // 注意 DevTools 前端必须与 devtools.html 同源同协议：iframe 跨源加载 https 前端时，
  // 内部 ws://127.0.0.1 会被浏览器当第三方 mixed content 拦截（报「调试连接已关闭」）。
  // 如确有需要（前后端分离/自定义 CDN），可填完整 URL 或同域路径强制指定。
  devtoolsPanel: "",
};
