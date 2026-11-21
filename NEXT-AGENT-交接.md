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
- 开发分支：`dev-接手-2026-11-19`、`dev-多租户-2026-11-20`（当前）
- 当前 HEAD：以 `git log -1` 为准；最近代码提交是 `d511fa3`（fix: 重新生成 TOTP 密钥先挂起确认），随后是 `a2d74ad`（docs: 记录 2026-11-20 停手点）和 `39fe89f`（docs: 增加 EV指挥官核心功能设计草案）。
- 工作区应无未提交的代码改动；`验收-本地基线-2026-11-19/` 保持未跟踪（截图/JSON，不提交）；项目根目录未跟踪的 `CLAUDE.md` 保留本地。
- 已验证基线：`3da5376` 一直保留；随时可以 `git switch main` 回到前辈版本。

关键提交：

```text
d511fa3 fix: 重新生成 TOTP 密钥先挂起确认
bb5ae48 feat: 增加 2FA、登录锁定与业务审计
44e98c1 feat: 增强 WhatsApp 媒体、发件箱与权限
7f57fd4 fix: 管理页补充 favicon 链接
9909b16 fix: 登录页公开品牌图标并补齐邀请拒绝读取体
ac06c09 fix: 拒绝请求前读取请求体并移除固定测试口令
b51ab5e fix: 管理端保留最后管理员并预校验口令
3d1bcd2 feat: 增加多租户管理后台、租户管理员与审计
f32e24e feat: 增加 WhatsApp 官方 Cloud API 服务端骨架
5c65706 feat: 增加多租户数据隔离与数据库登录会话
3075c39 refactor: 抽取鉴权与错误响应辅助方法
450a719 test: 补充 HTTP 边界用例
4adca26 test: 补充各模块失败路径回归用例
1e9d609 fix: 业务画像保存要求主营产品非空
d7a24ef fix: 限定仅 DELETE 允许空 body
80ff100 refactor: 抽取 HTTP 路由表并修复空 body DELETE
76635c8 docs: 记录 GEO runs 验收与提交
dc2b116 feat: 增加 GEO 问题库与模拟体检
ae51eee docs: 记录账单与获客占位进展
f05cfcb feat: 完成账单卡密与获客占位
a52d9f6 docs: 记录多语言禁改约束
b11ed8f docs: 记录图片模块收尾
58e882d feat: 完成图片蒙版与参考图收尾
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
- 安全与路由回归测试：`cd evcommander && python -m unittest discover -s tests -v`（**102 个用例**，覆盖图片注入/SSRF/支付幂等/mock 默认/占位接口/GEO 问题库与体检、HTTP 路由/边界、失败路径、多租户隔离/账号会话/旧库迁移、WhatsApp 账号/webhook/发送/翻译/自动回复/媒体/发件箱/限频/权限、管理后台/owner 权限/额度/审计、2FA/登录锁定/业务审计）。

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

### 4.8 图片蒙版与参考图收尾（`58e882d`，docs `b11ed8f`）

- 「局部重绘」预设 `image-inpaint`，`mask_mode:"native"`；蒙版转内部 Path 后传给 curl；
- 蒙版链路 UI 实测生成成功；参考图支持相对 `/material-media/`、`/generated-media/` 路径；
- 新增 `EV_IMAGE_EDIT_TIMEOUT`（默认 300 秒）；错误提示显示 curl stderr 摘要；
- 详见运行记录第十四节。

### 4.9 账单/资产与获客占位（`f05cfcb`，docs `ae51eee`）

- `/mb/balance`：账单字段齐全（title/createtime/direction/amount/power/type），累计充值消费正确；
- `/mb/card-exchange`：卡密兑换到账，余额与账单实测正常；
- `acquisition.py`：获客渠道占位，地图/天眼查/网页/视频/达人页面不再 4xx/5xx；未配置渠道明确报错；
- 详见运行记录第十五节。

### 4.10 GEO 问题库与模拟体检（`dc2b116`）

- `evcommander/geo_runs.py`：问题库增删查/生成、体检估算、模拟体检（文本模型模拟，引擎名带「（模拟）」）、回答详情、矩阵、报告；每次最多 8 条回答，失败可重试、不计积分；
- `geo.py` 接入问题库/体检/矩阵/报告路由，项目 counts 增加 runs；
- 页面验收通过：8 个 GEO 子页面均 200，体检链路正常（run 2 六条全成功；run 3 五条成功、一条上游偶发失败，页面有重试提示）；详见运行记录第十七节。

### 4.11 路由表抽取与 HTTP 路由回归（`80ff100`）

- `serve.py`：`send_head`/`do_POST` 的 if 链改为有序 `READ_ROUTES`/`WRITE_ROUTES` 路由表，分支抽成 `serve_*` 方法，行为顺序不变；
- 修复：无 body 的 DELETE（前端 axios `delete` 默认不带 data）不再被 400 拒绝；**仅 DELETE 允许空 body**，POST/PUT 空 body 仍 400，避免空 body 误清空业务画像/记忆；
- 新增 `tests/test_routes.py`：14 个真实 HTTP 用例（真实服务 + 临时数据目录），覆盖读/写路由、鉴权、别名、空 body 规则、兜底；
- 业务画像保存要求 `main_products` 非空（与前端一致），空 JSON/空字段/别名路径不会再把已有画像覆盖为空（`1e9d609`）；
- 复检：全量测试多轮重复 + 稳定压测均通过；旧版 `76635c8` 与新版 28 个请求对比状态/JSON 结构一致（唯一差异是有意修复的空 body DELETE）；
- 详见运行记录第十八节。

### 4.12 失败路径回归（`4adca26`）

- 新增 `tests/test_failures.py`：11 个模块级失败路径用例，覆盖 GEO 项目上限/不存在、支付取消与卡密/充值错误、知识库/记忆非法 id、数字人缺参数、客户导入与素材上传非法输入；
- `tests/test_routes.py` 补 2 个 HTTP 失败用例（DELETE 非 JSON body 400、未匹配写 API 503 兜底），未登录 PUT/DELETE 403 并入现有用例；
- HTTP 边界用例（`450a719`）：超大 JSON body 400、上传超限 413、非法 Content-Length 400、HEAD 无 body 与未登录 403；
- `serve.py` 新增 `denied`/`invalid`/`failed`/`send_denied`/`send_invalid` 辅助方法，并修复拒绝非 JSON 请求时未读 body 导致 Windows 客户端 RST 的问题（`3075c39`）；
- 所有失败路径均按预期拒绝；详见运行记录第十九节。

### 4.13 多租户第一阶段（`5c65706`）

- 分支：`dev-多租户-2026-11-20`；部署模型：**同一实例 + 共享库 + `tenant_id`**；账号由管理员 CLI 开通，无自助注册。
- 新增 `evcommander/tenancy.py`：
  - `tenants`、`users`、`sessions` 表；请求级租户上下文；密码 `scrypt`；会话 8 小时、数据库只存 token 哈希；
  - 旧库自动迁移：所有业务表补 `tenant_id`，现有数据归入演示租户 `demo`；不适合多租户的旧约束表（`pay_account`、`commander_memory`、`trial_usage`、`image_usage`、`acquisition_profile`、`operations_projects`）重建并保留数据。
- 新增 `evcommander/tenant_admin.py`：
  - `python tenant_admin.py tenant list|create --name ...`
  - `python tenant_admin.py user list [--tenant <id>]`
  - `python tenant_admin.py user create --tenant <id> --username <name> [--role member|admin]`
  - `python tenant_admin.py user password|disable|enable --username <name>`
  - `python tenant_admin.py seed-invite`（把现有 `EV_INVITE_CONFIG` 账号导入为平台管理员）
  - 口令只从提示或 stdin 读取，不写入命令历史/文档/git；数据库目录同样由 `EV_DATA_DIR` 决定。
- 登录：`invitation.py` 仍使用原登录页、cookie 名与 cookie 签名密钥；改为数据库账号 + `sessions` 表；既有邀请账号在用户表为空时自动导入（演示租户、`admin`），原口令可继续登录；旧 cookie 会失效，部署后重新登录一次。
- 租户隔离：各模块读写都按当前租户过滤；算力包、支付渠道、卡密为全平台共享（兑换码全局一次性）；余额、订单、账单、资产、客户、GEO、素材、知识库、记忆、灵感、调度、运营、生图作品/额度、试用额度均按租户隔离。
- 管理员跨租户：请求头 `X-Tenant: <tenant-id>`，仅 `admin` 角色且租户存在时生效；前端不发该头，普通账号自动忽略。
- 本地预览模式（无 `EV_TRIAL_ORIGIN`）固定使用演示租户，行为与之前一致。
- 测试：`tests/test_tenancy.py` 10 个用例 + `tests/test_routes.py` 1 个 `X-Tenant` 用例；全量 **64 个通过**。
- 页面验收：9 个主页面 + 8 个 GEO 子页面全部 HTTP 200，控制台错误与既有基线一致；真实本地库迁移后数据完整（详见运行记录第二十一节）。

### 4.14 WhatsApp 官方 Cloud API 服务端骨架（`f32e24e`）

- 新增 `evcommander/whatsapp.py`：
  - `wa_accounts`：每租户多个 Cloud API 账号（Phone Number ID、WABA ID、Access Token、App Secret、Webhook 验证口令、API 版本）；密钥不返回前端；
  - `wa_threads` / `wa_messages`：客户、最近消息、未读数、人工接管、发送/送达状态、翻译结果；
  - `wa_roles`：人设名称/提示/目标/知识库/模型/回复长度；
  - 全部表带 `tenant_id`，按当前租户隔离。
- 前端契约已接入（实际请求前缀 `/scrm-api/api/scrm/...`）：channels、accounts（CRUD/probe/reset-token）、inbox threads/messages/channel-counts/stats、takeover/send/translate/translate-draft、onboarding state/enable/disable/takeover、roles（CRUD）。
- 公开 webhook `/webhook/whatsapp`：
  - GET 用账号验证口令校验 `hub.mode=subscribe` 并回 `hub.challenge`；
  - POST 用该账号 App Secret 校验 `X-Hub-Signature-256`，按 `phone_number_id` 定位租户，落库消息/送达状态；
  - 重复消息按 `wa_id` 去重；账号停止值守后不再收新消息，送达状态仍更新。
- 发送：配置齐全时调用 Graph API；未配置明确报错。发送成功记录消息并置为人工接管。
- 自动回复/翻译复用现有文本模型；`send_template()` 为后续模板群发预留。
- 管理员 CLI `whatsapp_admin.py`：`list/show/create/update/delete/probe/reset-token`；密钥只从提示或 stdin 读取，列表不显示密钥。
- 环境变量：`EV_WHATSAPP_GRAPH_BASE`（默认 `https://graph.facebook.com`）、`EV_WHATSAPP_API_VERSION`（默认 `v22.0`）、`EV_WHATSAPP_TIMEOUT`（默认 20 秒）。
- 测试：新增 17 个 WhatsApp 用例 + 1 个 HTTP 路由用例；全量 **82 个通过**。
- 页面验收：临时库配置账号 + 签名 webhook 写入客户消息后，`/mb/global/whatsapp` 与 `/mb/scrm` 均 200、控制台 0 错误，会话/消息正常显示。
- 尚未拿 Meta 凭据，真实 Graph API/webhook 未联调；前端扫码按钮仍是客户端模式（Cloud API 账号用 CLI/API 配置），详见运行记录第二十二节。
- 媒体下载、发件箱/限频、自动回复去重、账号管理权限已在 4.16 增强；本节早期的“媒体只占位、自动回复每消息起线程、所有成员可管账号”等限制已不再适用。

### 4.15 多租户第二阶段（`3d1bcd2`，修复 `b51ab5e`）

- 角色：`admin`（平台管理员）、`owner`（租户管理员）、`member`（普通账号）。
  - `admin`：跨租户管理租户/账号/审计；
  - `owner`：仅本租户普通账号与本租户审计/概览；
  - `member`：无管理 API/页面权限。
- 新增 `evcommander/admin.py`：
  - API：`/api/admin/me`、`overview`、`tenants`、`users`、`audit`；
  - 后端生成的独立管理页面（不依赖 `mirror/`）：概览、租户、账号、审计；开通租户/账号、改角色/状态、重置口令（密码弹窗）；
  - 页面路径由 `EV_ADMIN_PATH` 配置（必须以 `/admin` 开头，留空关闭）；**实际值不写进代码/文档/提交**。
- 租户额度：`tenants.max_users`（默认 5，演示租户 50）；创建账号按启用账号数校验上限。
- 审计：`audit_log` 记录 `tenant.create/update`、`user.create/role/password/active/disabled`；不记录口令/密钥；平台管理员看全部，owner 看本租户。
- CLI `tenant_admin.py` 增加：`tenant update`（名称/状态/账号上限）、`user role`、`audit`；CLI 操作人记为“管理员CLI”。
- 停用账号/租户会清理会话；不能停用/降级当前账号；最后一个启用的平台管理员不能被降级/停用（API/CLI/直连均保护，`b51ab5e`）；账号更新先校验口令再改角色/状态。
- 迁移：旧库自动补 `tenants.max_users`（演示租户 50）并创建 `audit_log`；业务数据不动。
- 测试：新增 8 个 admin 用例 + 1 个 HTTP 用例；全量 **91 个通过**。
- 页面验收：平台管理员开通租户/账号、重置口令、审计实时更新；owner 登录后看不到租户管理区、只能看本租户账号；两页控制台 0 错误。
- 限制：租户内数据仍是同租户共享，未做用户/部门二次隔离；审计只覆盖管理动作，不覆盖业务操作。
- 多轮检查修复（`ac06c09`/`9909b16`/`7f57fd4`）：未登录/被拒绝 POST 先读小请求体避免 Windows RST；测试口令改运行时随机；公开 favicon/logo 避免邀请页 401；旧库先补 `max_users` 再插入。
- 复检：全量 91 连跑 3 轮、路由 40 轮、admin 5 轮全部通过；管理后台/owner/WhatsApp/SCRM/GEO/9 主页面验收通过；真实库哈希未变。

### 4.16 WhatsApp 服务端增强（`44e98c1`）

- 媒体：入站媒体 `media_id` 由后台线程调用 Graph API 下载，归档到素材库并回填 `media_url`；HTTPS/私网校验、大小上限、可选域名白名单；失败保留占位。
- 发件箱：新增 `wa_outbox`，手动/自动回复先入队；后台 worker 串行发送、每账号默认 30 条/分钟（`EV_WHATSAPP_RATE_LIMIT`）、失败 3 次退避重试、重启恢复；人工接管后取消未发出的 AI 回复。
- 自动回复按“租户+会话”去重，同一会话并发只生成一条；失败写账号 `last_error`。
- 权限：WhatsApp 账号/人设/值守写操作仅 `admin`/`owner`；成员保留查看、发送、接管、翻译。
- 新环境变量：`EV_WHATSAPP_MEDIA_MAX`（默认 32 MB）、`EV_WHATSAPP_MEDIA_HOSTS`（可选逗号白名单）、`EV_WHATSAPP_MEDIA_FETCH`（0 关闭）、`EV_WHATSAPP_RATE_LIMIT`（默认 30/分钟/账号，0 关闭）。
- 测试：WhatsApp 17→24 个用例；全量 **98 个通过**；路由 10 轮 0 失败；WhatsApp/SCRM 页面 200、0 控制台错误。
- 限制：发件箱 worker 为单进程线程（单实例部署）；媒体 DNS 私网校验未做，生产建议配白名单；模板仍只有 helper。
- 详见运行记录第二十五节。

### 4.17 登录安全与业务审计（`bb5ae48`，修复 `d511fa3`）

- 2FA（TOTP）：标准 SHA1/6 位/30 秒/±1 步；`users.totp_secret/totp_pending/totp_enabled`；登录页增加可选动态码字段；管理页「我的登录安全」可生成/启用/关闭；重新生成先写 `totp_pending`，确认后才替换（`d511fa3`）；账号表可重置 2FA；CLI `user totp-*`。
- 登录日志/锁定：`login_log` 记录成功/失败/原因/IP；同账号 15 分钟 5 次失败锁定；管理员/CLI 可解锁；管理页「登录记录」按租户筛选。
- 业务审计：`tenancy.audit_in` 在业务事务内记录订单创建/支付/取消/卡密/资产、GEO 项目/问题/体检、WhatsApp 账号/人设/值守/入队；不记录口令/Token/验证码/消息正文。
- 测试：新增 4 个用例 + 审计断言；全量 **102 个通过**；路由 10 轮 0 失败。
- 页面验收：邀请模式无码登录失败、带码登录成功，管理页显示 2FA 与登录记录；本地预览管理页隐藏 2FA 区，0 控制台错误。
- 限制：TOTP 密钥明文存 DB、无恢复码、2FA 仅 admin/owner 自助设置、锁定按账号不按 IP、代理后登录 IP 为代理地址、业务审计未做归档。
- 详见运行记录第二十六节。

### 4.18 产品核心功能设计草案（2026-11-21，文档）

- 文件：`EV指挥官-核心功能设计-2026-11-21.md`（仓库根目录）。
- 定位建议：面向中小企业的 AI 数字员工与获客经营平台；核心链「获客 → 客户 → 触达 → 内容 → 品牌曝光 → 经营复盘」。
- **P0 七项**：AI 大脑、获客→客户中心、WhatsApp/SCRM、图片生产、充值/订单、多租户管理后台、GEO 模拟体检。
- **P1 七项**：GEO 真实采集、渠道获客、数字人 TTS/视频、内容工坊/品牌知识页、经营协同完善、官网线索同步、用户/部门级隔离。
- **P2 暂缓**：设备农场/PC 助理、短剧/直播/KOL/电商等视频类、增长/支持/人才等空壳页面——统一“暂未开放”，不假成功。
- 待用户拍板 7 项：目标客户、MVP 范围、算力单价、GEO 模拟是否对外、暂缓页面处理、用户级隔离时机、商业化模式。
- 建议拍板后先补“**算力消费扣费**”：当前对话/生图/GEO 体检尚未扣算力，是商业闭环缺口。

## 5. 当前本地测试数据

- SQLite：`evcommander/.local/commander.sqlite3`
- 已迁移为多租户结构（演示租户 `demo`，另有 `tenants`、`users`、`sessions` 表）：
  - 1 个 GEO 项目 `TestBrand`
  - 1 条获客线索“测试客户A”（电话 13800000001，来源 map）
  - 6 笔充值订单、7 条账单，余额 1550、累计充值 120
  - 2 个本地会话、13 个素材文件、10 张生图作品
- 迁移前备份：`evcommander/.local/commander.sqlite3.pre-tenancy-2026-11-20.bak`（未跟踪，保留本地）。
- 本地预览模式不需要登录，固定使用演示租户；如需模拟多租户，用 `tenant_admin.py` 建租户/账号，再启动 invite 模式。
- WhatsApp 相关表（`wa_accounts`/`wa_threads`/`wa_messages`/`wa_roles`）在首次访问时创建；真实本地库当前没有 WhatsApp 账号（验收用临时库）。
- 多租户第二阶段：真实库下次启动会自动补 `tenants.max_users`（demo=50）并创建 `audit_log`；管理页面只有在设置 `EV_ADMIN_PATH` 时才提供服务。
- WhatsApp 增强：下次启动会创建 `wa_outbox` 并给旧 `wa_messages` 补 `media_id`；媒体文件归档到素材库（`DATA_DIR/material-media`），发件箱 worker 单进程运行。
- 登录安全：下次启动会补 `users.totp_secret/totp_enabled` 并创建 `login_log`；现有账号默认未开启 2FA。
- 管理审计账号/口令：测试口令只存在于临时测试库，不写进文档/提交。
- 需要干净演示时，可以删除 `.local/commander.sqlite3` 后重启（会失去所有测试数据，并重建演示租户）。
- `验收-本地基线-2026-11-19/` 是未跟踪的截图/JSON，保留本地即可。

## 6. 接下来做什么

**决策已于 2026-11-20 拍板，见 `决策单-待确认事项-2026-11-19.md` 顶部「拍板结果」。多租户第一阶段已于 2026-11-20 完成（`5c65706`）。**

**2026-11-21 新增：产品核心功能设计草案已出（`EV指挥官-核心功能设计-2026-11-21.md`），等用户拍板；拍板前不写代码。拍板后建议先补“算力消费扣费”（详见运行记录第二十七节）。**

### 6.1 下一步：WhatsApp 官方 Cloud API 联调

服务端骨架已完成（`f32e24e`），现在缺 Meta 侧凭据和公网回调：

1. 准备 Meta 商业账号、WhatsApp 号码、App ID/Secret、Phone Number ID、长期 Access Token、验证口令；
2. 用 `python whatsapp_admin.py create --tenant <id> --name <名称> --phone-number-id <id>` 配置账号；
3. 在 Meta 后台配置回调 `https://<域名>/webhook/whatsapp`，订阅 `messages` 字段；
4. `python whatsapp_admin.py probe --id <id>` 验证连通；发真实消息检查收件箱、送达状态、人工接管；
5. 需要模板消息时再补前端/群发流程；**不要**先做网页自动化或设备农场（已拍板先不做）。

### 6.2 之后按顺序

1. **数字人**：视频合成；需要视频/口型供应商和预算，没有供应商前继续文稿/TTS 占位；
2. **GEO 真实采集**：需要中转站支持哪些引擎、是否联网搜索、key/预算；报告按实际能力标注；
3. **多租户第三阶段（可选）**：租户内按用户/部门隔离、业务操作审计、2FA、租户自助续费/账单发票；
4. **设备农场 / PC 助理**：先不做；
5. **支付**：继续 mock，不接真实渠道。

## 7. 前端契约速查

- 支付：`/agent-api/pay/*`、`/agent-api/order/*`、`/agent-api/bill/*`、`/agent-api/card/*`、`/agent-api/asset/*`；响应需要 `{code:"0000",msg:"",data:...}`。
- 获客/CRM：`/agent-api/api/acquisition/*`、`/scrm-api/api/scrm/contacts*`；响应多为直接对象。
- GEO：`/agent-api/api/geo/*`；响应直接 `{ok:true,...}`。
- 图片：`/api/ext/designer/image/stream`、`/api/ext/ecom-model/image-edit/stream` 等，SSE 事件 `delta/data/usage/error`。
- 登录：`GET /invite` 登录页，`POST /invite` 表单登录（`username/password/code`，`code` 为可选 TOTP），`POST /sign-out` 退出；cookie 名 `ev_commander_session`。登录失败 15 分钟 5 次锁定，管理员可解锁。
- 多租户：前端契约不变，数据按登录会话的租户过滤；本地预览固定演示租户。管理员联调可用请求头 `X-Tenant: <tenant-id>` 切租户，普通账号忽略。
- WhatsApp：前端实际请求 `/scrm-api/api/scrm/...`（scrmService 的 baseURL 是 `/scrm-api`）；webhook 是公开路径 `/webhook/whatsapp`，GET 用验证口令、POST 用 App Secret 校验签名；响应结构见 `whatsapp.py` 的 `read`/`write`。媒体消息的 `media_url` 指向 `/material-media/<key>`；发送先入 `wa_outbox` 队列，前端按 `deliver_status` 轮询 pending/sent/failed。
- 管理后台：页面路径由 `EV_ADMIN_PATH` 配置（留空关闭）；API 为 `/api/admin/me|overview|tenants|users|audit|logins`，以及 `/api/admin/me/security`、`/api/admin/me/totp/setup|enable|disable`；需要 `admin`/`owner` 会话；`mirror/` 不参与。
- 路由入口：`evcommander/serve.py` 的 `send_head`/`do_POST`，以及类后的有序 `READ_ROUTES`/`WRITE_ROUTES` 路由表。

## 8. 给下一个 Agent 的复制提示词

```text
请接手 D:\EVOL-DREAM-线上版-20261119-交接 这个项目。

先按顺序读：
1. NEXT-AGENT-交接.md
2. 运行记录-2026-11-19.md
3. 接手分析-2026-11-19.md
4. 接口契约-7模块-2026-11-19.md

当前分支 dev-多租户-2026-11-20，HEAD 以 git log -1 为准（最近代码提交 d511fa3），基线 tag live-2026-11-19 (3da5376)。

硬性原则：
- 不要改 evcommander/mirror/ 下任何前端文件；
- 后端必须适配现有前端；
- 每完成一个模块更新运行记录并分别提交代码/文档；
- 不提交测试 key、密码、后台安全入口。

当前已完成：文本模型 provider、参考图/图片编辑（含蒙版）、充值订单、获客核心与渠道占位、GEO 项目骨架与模拟体检、账单/卡密兑换、路由表抽取与 HTTP 路由回归、多租户第一阶段（tenant_id 数据隔离 + 数据库登录会话 + 管理员 CLI）、WhatsApp 官方 Cloud API 服务端骨架、**多租户第二阶段（管理后台 + owner 权限 + 租户额度 + 审计）**。
工作区应无未提交代码；102 个单元测试通过（路由测试复检通过）。
产品核心功能设计草案已出：`EV指挥官-核心功能设计-2026-11-21.md`，等用户拍板；拍板后建议先补“算力消费扣费”，再做 WhatsApp 联调（需要 Meta 凭据）。
下一步是 WhatsApp 联调：需要 Meta 商业账号、号码、App Secret、Phone Number ID、Access Token、验证口令和公网 webhook；服务端已就绪，见运行记录第二十二节。
启动命令、测试 API 结论、已知问题都在 NEXT-AGENT-交接.md 和运行记录里。
```
