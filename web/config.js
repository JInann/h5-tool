// h5-tool 全局配置（前后端分离模式）
//
// 本地单机模式（直接打开 http://127.0.0.1:12787/）无需修改任何配置；
// 部署到服务器后，浏览器打开服务器页面，页面里的 JS 会请求下面的 backend 地址。
// 注意：127.0.0.1 指「浏览器所在的那台电脑」（即使用者本机，后端跑在那里），不是服务器。
window.H5TOOL_CONFIG = {
  // 后端地址：h5-tool Python 后端，跑在使用者自己电脑上（127.0.0.1:12787）
  backend: "http://127.0.0.1:12787",
  // DevTools 面板资源入口（devtools-frontend 的 inspector.html，已部署到 CloudBase 静态托管）
  devtoolsPanel: "https://devtools-xhstudy-d1g9ap809fb38788a.webapps.tcloudbase.com/front_end/inspector.html",
};
