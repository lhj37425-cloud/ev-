# EVOL DREAM 三语官网

基于已确认效果图开发的中文、越南语和英语公司网站，包含培训、Agent 部署、项目实施、场景与咨询流程。

## 本地运行

需要 Node.js >= 22.22.3。

```sh
npm ci
npm run build
npm start
```

默认打开 http://127.0.0.1:4321/ 会进入越南语首页。中文 http://127.0.0.1:4321/zh/ ，越南语 http://127.0.0.1:4321/vi/ ，英语 http://127.0.0.1:4321/en/ 。页头菜单可切换语言。服务只监听本机。咨询表单真实保存到 `.local/leads/`，当前不发送业务通知。

`npm run dev` 提供页面热更新，不包含咨询 API；完整功能使用上面的构建与启动命令。

## 检查与线索

```sh
npm run check
npm test
npm run leads
node server/leads.mjs csv > .local/leads-export.csv
```

开发说明、页面清单与后续批次见 [本地预览说明](../03_中文版开发与本地预览说明.md)。

三语开发与验收记录见 [三语版本开发记录](../05_三语版本开发记录.md)。

## 开源来源

基于 [AstroWind](https://github.com/arthelokyo/astrowind) 提交 `14e1a691f80548dcc36370847b1a02c0d0b12821` 开发，保留 [MIT 许可证](LICENSE.md)。品牌界面、中文内容、本地咨询服务已按本项目重建。
