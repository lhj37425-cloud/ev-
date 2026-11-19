# 给下一个 Agent 的交接说明（2026-11-19）

> 目标：下一个 Agent 不读当前对话，也能安全接手、继续开发、验收和回退。

## 0. 第一件事：按顺序读这些文件

1. 本文件
2. `运行记录-2026-11-19.md`（进度、启动命令、已做/未做、已知问题）
3. `接手分析-2026-11-19.md`（交接包结构、7 个模块范围、测试 API 结论）
4. `接口契约-7模块-2026-11-19.md`（前端实际路径、字段、SSE 事件）
5. `决策单-待确认事项-2026-11-19.md`（需要前辈/老板拍板的事项）
6. `后端交接清单.md`（前辈给的原始范围）

## 1. 仓库与分支

- 项目目录：`D:\EVOL-DREAM-线上版-20261119-交接`
- 基线：tag `live-2026-11-19`，commit `3da5376`，分支 `main`
- 开发分支：`dev-接手-2026-11-19`
- 当前 HEAD：以 `git log -1` 为准；最近代码提交是 `aa8db8f`（P0 安全修复）
- 已验证基线：`3da5376` 一直保留；随时可以 `git switch main` 回到前辈版本。

关键提交：

```text
aa8db8f fix: 修复图片注入、SSRF、支付重复到账和假成功接口
e54cf7f docs: 记录 GEO 模块进展
ffd2e41 feat: 增加 GEO 项目与页面骨架
d0ee71d docs: 记录获客模块进展
6bb0ce3 feat: 增加获客线索保存与流转
cda2743 docs: 记录充值订单模块进展
8477b25 feat: 增加充值与订单后端
efcaead docs: 记录提交号与回退方式
ff09936 docs: 补充接手分析、接口契约和运行记录
34904b4 feat: 增加图片生成与编辑后端
e0d23b4 feat: 增加可配置的文本模型 provider
3da5376 (tag: live-2026-11-19) 前辈交接基线
```

### 回退

```sh
git switch main                      # 看/跑前辈版本
git switch dev-接手-2026-11-19       # 回到开发版
git reset --hard live-2026-11-19     # 丢弃开发提交（慎用）
```

未跟踪的 `验收-本地基线-2026-11-19/` 和 `evcommander/.local/` 不会被切换/重置删除；不要运行 `git clean -fd`。

## 2. 硬性原则

1. **不要改 `evcommander/mirror/` 下任何前端文件。** 前端是编译产物，后端必须适配前端。
2. 每完成一个模块：更新 `运行记录-2026-11-19.md`，然后提交一次代码 + 一次文档。
3. 涉及真实收款、停用模型、改共享规则前，先让用户确认。
4. 测试 key、密码、后台安全入口不写进代码/文档/提交。
5. 每步用现有页面 + Playwright 验收，不要靠“接口返回 200”判断前端可用。
6. **多语言未优化，不要动**：不要修改 `evcommander/mirror/` 的 i18n/语言资源、`website/` 三语文案、翻译文件或语言切换逻辑。后端提示可以用中文，但不要新增/修改前端多语言键。

## 3. 当前能跑的东西

### 3.1 服务

| 服务 | 地址 | 启动 |
|---|---|---|
| 官网前台 | `http://127.0.0.1:4173/zh/` | `cd website && LOCAL_HEPH_TRIAL=1 node server.js` |
| 官网后台 | `http://127.0.0.1:4181/` | `cd website && node backend-server.js` |
| EV 指挥官 | `http://127.0.0.1:8766/mb/home` | 见下面命令 |

EV 指挥官启动命令（Windows Git Bash）：

```sh
cd evcommander
EV_LLM_BASE_URL='https://www.roookey.com/v1' \
EV_LLM_MODEL='gemini-3.8-flash-gcp' \
EV_LLM_PROTOCOL='chat_completions' \
EV_LLM_LABEL='Gemini 3.8 Flash（测试）' \
EV_LLM_BRAND='Google' \
EV_LLM_MAX_TOKENS='4096' \
EV_LLM_API_KEY_FILE='D:/EVOL-DREAM-线上版-20261119-交接/evcommander/.local/llm.key' \
EV_IMAGE_BASE_URL='https://www.roookey.com/v1' \
EV_IMAGE_MODEL='gpt-image-2' \
EV_IMAGE_NAME='GPT Image 2' \
EV_IMAGE_BRAND='OpenAI' \
EV_IMAGE_QUALITY='low' \
EV_IMAGE_API_KEY_FILE='D:/EVOL-DREAM-线上版-20261119-交接/evcommander/.local/llm.key' \
EV_IMAGE_FETCH_HOSTS='uskaichi.tos-accelerate.bytepluses.com.cn' \
EV_IMAGE_DAILY_LIMIT='100' \
EV_IMAGE_CONCURRENCY='1' \
EV_PAY_MOCK='1' \
python serve.py --port 8766
```

注意：

- `EV_PAY_MOCK=1` 只用于本地测试；生产默认 `0`，不要带这个变量上线；
- `EV_IMAGE_FETCH_HOSTS` 是图片 CDN 白名单，relay 返回的图片主机必须包含；生产按实际 CDN 配置；
- 8765 被本机另一个程序 `daily-dissect` 占用，所以用 8766；
- 官网 `/zh/agents/` 的“试用 EV指挥官”按钮仍指向 8765，直接开 8766；
- 测试 key 在 `evcommander/.local/llm.key`，被 `.gitignore` 忽略；如果文件不存在，向用户要测试 key，不要自己编；
- `evcommander/.local/` 还包含测试 SQLite、上传素材、生成图片，不要提交。

### 3.2 测试 API 结论（重要）

- 可用文本模型：`gemini-3.8-flash-gcp`，走 `POST /v1/chat/completions`，流式正常；
- `gemini-3.8-flash`、`gemini-3.8-flash-vt` 无可用渠道；
- relay 不支持 `/v1/responses` 给 Gemini，会报 `contents is not specified`；
- 可用图片模型：`gpt-image-2`，`/v1/images/generations` 和 `/v1/images/edits` 都可用，返回 URL（不是 b64_json），`EV_IMAGE_QUALITY=low` 较快；
- 不要把 key 写进代码或文档。
- 安全回归测试：`cd evcommander && python -m unittest discover -s tests -v`（12 个用例，覆盖图片注入/SSRF/支付幂等/mock 默认/占位接口）。

## 4. 已完成模块

### 4.1 文本模型 provider（`e0d23b4`）

- 文件：`evcommander/llm_provider.py`、`commander.py`
- 支持 `responses` 和 `chat_completions` 两种协议；
- 聊天 `/mb/brain` 已用 `gemini-3.8-flash-gcp` 实测收发成功。

### 4.2 参考图和图片编辑（`34904b4`）

- 文件：`image_generation.py`、`image_studio.py`、`materials.py`、`serve.py`
- 支持文字生图、参考图、图片编辑、试装/换装、商品换背景；
- 兼容 URL/b64 返回、本地 `/material-media/` 上传文件、`/api` 与 `/agent-api/api` 双前缀；
- `/mb/production/image-edit` 实测：上传原图 + 修改要求，生成成功并归档到作品库；
- 「局部重绘」：画布加载、涂抹/框选、提交生成已通过 UI 实测；第一次遇到 relay connection reset，重试成功；
- 参考图：`/api/ext/designer/image/stream` 接受相对 `/material-media/`、`/generated-media/` 参考图；
- `product-bg`：`EV_IMAGE_EDIT_TIMEOUT=300` 下实测成功；
- 已知：上游偶尔 connection reset；图片额度和并发仍全站共享，按用户隔离等身份模型。

### 4.3 充值和订单（`8477b25`）

- 文件：`payments.py`、`serve.py`
- 算力包、支付渠道、订单、账单、兑换码、资产、余额；
- 本地 mock 支付：6 秒后自动到账，`EV_PAY_MOCK=0` 可关闭；
- `/mb/recharge`、`/mb/order` 实测：下单、二维码、支付成功、余额到账、订单列表、取消订单都正常；
- `/mb/balance` 实测：账单字段、累计充值/消费正确；
- `/mb/card-exchange` 实测：`{card_no,card_password}` 兑换成功，余额和账单到账；
- 已知：没有接真实支付渠道/回调验签/对账；消费扣费还没接。

### 4.4 获客核心（`6bb0ce3`）

- 文件：`customers.py`、`serve.py`
- 支持外部线索批量保存 `/agent-api/api/acquisition/leads/save`；
- 支持放入公共池、认领、转让（当前工作区）、联系人微信标记；
- `/mb/acquisition/leads` 实测：测试客户 A 正常显示，线索流转接口通过；
- 共享/取消合并/AI 去重/外部调研现在明确返回“暂未开放”，不再假成功；
- `acquisition.py` 补了渠道配置占位：地图/天眼查/网页/视频/达人页面不再 503；业务画像可本地保存；搜索/采集明确返回“未配置获客渠道”；
- 已知：真正的外部渠道采集仍未接，需要 provider 账号/API key/RPA。

### 4.5 GEO 核心骨架（`ffd2e41`）

- 文件：`geo.py`、`serve.py`
- 支持 `meta`、项目 CRUD、`resume`；
- 品牌/体检/问题/报告/内容/发布/自动执行等子页面返回空数据，不再 400/503；
- `/geo` 实测：项目 `TestBrand` 显示，所有 GEO 子路由正常加载空状态；
- 已知：真正的 AI 引擎采集、回答解析、报告/诊断/自动执行未实现。

### 4.6 数字人文稿/翻译（当前 HEAD 提交）

- 文件：`digital_human.py`、`serve.py`
- `script/propose`：按主题/风格/时长/数量生成口播稿；
- `script/translate`：翻译口播稿；
- `/mb/production/digital-human` 的「AI 提案」已实测能生成 3 条稿子；
- 形象/音色/TTS/视频合成仍未接，需要供应商或本地资产，且受“AI 视频暂缓”影响。

### 4.7 P0 安全修复（`aa8db8f`）

- 图片：`--form-string`、模型白名单、只读内部媒体路径、公网 HTTPS + `EV_IMAGE_FETCH_HOSTS` 白名单，拒绝内网/HTTP。
- 支付：`EV_PAY_MOCK` 默认 `0`；测试兑换码只在 mock/演示环境写入；`BEGIN IMMEDIATE` + `rowcount == 1` 防止重复到账。
- 占位接口：共享/取消合并/AI 去重/外部调研明确返回“暂未开放”；多用户转让/移交也返回未开放。
- `geo.py` 的 `_aliases` 已拆为 `_path_aliases` / `_brand_aliases`。
- `chat_allowed` 同时接受 `127.0.0.1` 和 `localhost`。
- 12 个 `unittest` 回归用例：`cd evcommander && python -m unittest discover -s tests -v`。

## 5. 当前本地测试数据

- SQLite：`evcommander/.local/commander.sqlite3`
- 包含：
  - 1 个 GEO 项目 `TestBrand`
  - 1 条获客线索“测试客户A”（电话 13800000001，来源 map）
  - 若干充值订单，余额约 1500
- 需要干净演示时，可以删除 `.local/commander.sqlite3` 后重启（会失去所有测试数据）。
- `验收-本地基线-2026-11-19/` 是未跟踪的截图/JSON，保留本地即可。

## 6. 接下来做什么

**先做 `决策单-待确认事项-2026-11-19.md` 的第三步：把待拍板问题一次发给前辈/老板。** 在用户身份、支付、WhatsApp、设备、数字人、GEO 数据源确认前，不要直接开这些重模块。

按前辈清单剩余：

1. **WhatsApp**
   - 前端 `/mb/global/whatsapp` 明确要求“电脑客户端登录 WhatsApp 网页版，客户端必须在线”；
   - 需要先确认：官方 Cloud API，还是电脑客户端 WhatsApp Web 自动化；
   - 服务端至少要有账号、会话、消息、值守状态；客户端 Agent 另做。
2. **设备农场 / PC 助理**
   - `/api/machines/*`、`/api/desktop/*`、`/api/rpa/devices/*`；
   - 需要设备/PC 客户端 Agent、截图、RPA、RTC。
3. **数字人**
   - `/api/digital-human/*`、`/api/voice/*`、`/api/ext/livehost/*`；
   - 需要先确认数字人供应商、TTS/口型/视频合成方案；交接 README 说 AI 视频生成暂缓。
4. **GEO 深化**
   - 先确认 AI 引擎数据源（豆包/DeepSeek/元宝/Kimi 或 ChatGPT/Gemini/Claude/Perplexity）；
   - 再实现 runs/answers/mentions/reports。

## 7. 前端契约速查

- 支付：`/agent-api/pay/*`、`/agent-api/order/*`、`/agent-api/bill/*`、`/agent-api/card/*`、`/agent-api/asset/*`；响应需要 `{code:"0000",msg:"",data:...}`。
- 获客/CRM：`/agent-api/api/acquisition/*`、`/scrm-api/api/scrm/contacts*`；响应多为直接对象。
- GEO：`/agent-api/api/geo/*`；响应直接 `{ok:true,...}`。
- 图片：`/api/ext/designer/image/stream`、`/api/ext/ecom-model/image-edit/stream` 等，SSE 事件 `delta/data/usage/error`。
- 路由入口：`evcommander/serve.py` 的 `send_head` 和 `handle_chat_post`。

## 8. 给下一个 Agent 的复制提示词

```text
请接手 D:\EVOL-DREAM-线上版-20261119-交接 这个项目。

先按顺序读：
1. NEXT-AGENT-交接.md
2. 运行记录-2026-11-19.md
3. 接手分析-2026-11-19.md
4. 接口契约-7模块-2026-11-19.md

当前分支 dev-接手-2026-11-19，HEAD 以 git log -1 为准（最近代码提交 aa8db8f），基线 tag live-2026-11-19 (3da5376)。

硬性原则：
- 不要改 evcommander/mirror/ 下任何前端文件；
- 后端必须适配现有前端；
- 每完成一个模块更新运行记录并分别提交代码/文档；
- 不提交测试 key、密码、后台安全入口。

当前已完成：文本模型 provider、参考图/图片编辑、充值订单、获客核心、GEO 项目骨架。
下一步先确认 WhatsApp 走官方 Cloud API 还是电脑客户端 WhatsApp Web，然后继续实现。
启动命令、测试 API 结论、已知问题都在 NEXT-AGENT-交接.md 和运行记录里。
```
