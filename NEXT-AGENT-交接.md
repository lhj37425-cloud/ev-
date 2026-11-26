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
- 当前 HEAD：以 `git log -1` 为准；最近代码提交是 `1ed7711`（feat: 增加 GEO 官网工具通知搜索引擎（IndexNow/百度收录）），之前是 `7398374`（feat: 增加 GEO 官网工具商品清单与 Merchant Center 数据源）、`8862796`（feat: 增加 GEO 官网工具搜索平台报表导入）、`1673bd5`（feat: 增加 GEO 官网工具 AI 带来的访问（beacon 统计与留资））、`26e955b`（feat: 增加 GEO 官网工具 AI 来访记录（访问日志解析））、`02f5200`（feat: GEO 发布改为后台队列并支持重试、封面、分类标签与下线）、`89c65d5`（feat: 增加 GEO 发布渠道（WordPress/Webhook））、`cce96c2`（feat: 增加 GEO 被引用页面拆解）、`299cf91`（feat: 增加 GEO 官网体检与 AI 可读性生成器）、`1f291dd`（feat: 接通 GEO 引用解析与来源统计）、`ad906fb`（feat: 增加 GEO 品牌知识页与公开站点）、`eb90631`（feat: 增加 GEO 内容工坊与手工发布）、`356d6c3`（feat: 增加 GEO 诊断方案与告警）、`76d7303`（feat: 增加 GEO 报告详情与公开分享）、`072f61b`（feat: 增加 GEO 关键词矩阵后端）、`f700568`（feat: 增加 GEO 品牌事实与竞品后端）、`110df68`（feat: 对话改为按 token 扣算力）、`ae59c45`（feat: 接通对话、生图与GEO体检算力扣费）和 `d511fa3`（fix: 重新生成 TOTP 密钥先挂起确认）。
- 工作区应无未提交的代码改动；`验收-本地基线-2026-11-19/` 保持未跟踪（截图/JSON，不提交）；项目根目录未跟踪的 `CLAUDE.md` 保留本地。
- 停手点（2026-11-26）：代码提交 `1ed7711`，文档提交以 `git log -1` 为准；全量 **301/301 测试通过**（连跑 2 轮）；通知搜索引擎（密钥验证/IndexNow/百度配置与提交/历史）页面验收完成；**官网工具 7 个页签已全部完成**；验收服务已停止；真实 `.local/commander.sqlite3` 未被本次改写（业务数据未变，仅此前测试误触留下两张空表 `geo_channel_configs`、`geo_push_jobs`）。详见运行记录 45.5。
- 已验证基线：`3da5376` 一直保留；随时可以 `git switch main` 回到前辈版本。

关键提交：

```text
1ed7711 feat: 增加 GEO 官网工具通知搜索引擎（IndexNow/百度收录）
7398374 feat: 增加 GEO 官网工具商品清单与 Merchant Center 数据源
8862796 feat: 增加 GEO 官网工具搜索平台报表导入
1673bd5 feat: 增加 GEO 官网工具 AI 带来的访问（beacon 统计与留资）
26e955b feat: 增加 GEO 官网工具 AI 来访记录（访问日志解析）
02f5200 feat: GEO 发布改为后台队列并支持重试、封面、分类标签与下线
89c65d5 feat: 增加 GEO 发布渠道（WordPress/Webhook）
cce96c2 feat: 增加 GEO 被引用页面拆解
299cf91 feat: 增加 GEO 官网体检与 AI 可读性生成器
1f291dd feat: 接通 GEO 引用解析与来源统计
ad906fb feat: 增加 GEO 品牌知识页与公开站点
eb90631 feat: 增加 GEO 内容工坊与手工发布
356d6c3 feat: 增加 GEO 诊断方案与告警
76d7303 feat: 增加 GEO 报告详情与公开分享
072f61b feat: 增加 GEO 关键词矩阵后端
f700568 feat: 增加 GEO 品牌事实与竞品后端
110df68 feat: 对话改为按 token 扣算力
ae59c45 feat: 接通对话、生图与GEO体检算力扣费
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
- 安全与路由回归测试：`cd evcommander && python -m unittest discover -s tests -v`（**301 个用例**，覆盖图片注入/SSRF/支付幂等/mock 默认/占位接口/GEO 问题库与体检/GEO 品牌事实与竞品/声量份额/GEO 关键词矩阵/GEO 周报月报与公开分享/GEO 诊断方案与告警/GEO 内容工坊与手工发布/GEO 品牌知识页/GEO 引用解析（含页面拆解）/GEO 官网体检与生成器/GEO 发布渠道（provider 定义、接入 CRUD/密钥隐藏、连接测试、WordPress/Webhook 推送、后台队列与重试、封面图、分类/标签、下线、草稿、失败、跨租户、私网拒绝）、GEO 官网工具（体检/生成器/AI 来访记录上传解析与统计/AI 带来的访问 beacon 统计与留资/搜索平台报表导入/商品清单与 feed/通知搜索引擎 IndexNow 与百度收录）/HTTP 路由/边界/空 body 白名单、失败路径、多租户隔离/账号会话/旧库迁移、WhatsApp 账号/webhook/发送/翻译/自动回复/媒体/发件箱/限频/权限、管理后台/owner 权限/额度/审计、2FA/登录锁定/业务审计、对话按 token/生图/GEO 算力扣费与失败退款、中转站 usage 解析）。

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

### 4.19 算力消费扣费（`ae59c45`，`110df68` 改为按 token）

- 单价（2026-11-22 用户拍板）：对话**按 token**——输入 **0.05 算力/千 token**、输出 **0.2 算力/千 token**（`110df68`）；生图 **2 算力/张**、GEO 体检 **0.2 算力/条成功回答**；环境变量 `EV_PRICE_INPUT_PER_1K` / `EV_PRICE_OUTPUT_PER_1K` / `EV_PRICE_IMAGE` / `EV_PRICE_GEO_ANSWER` 可随时调整，`EV_POWER_CHARGE=0` 全局关闭，`EV_TRIAL_ORIGIN` 试用模式自动关闭扣费。
- `payments.py`：原子扣款（余额不足直接拒绝、不透支）、消费账单 `kind='consume'`（金额按 1 算力≈¥0.1 折算展示）、审计 `power.consume`/`power.refund`；账单已用支持小数。
- 对话：按“输入估算 + 输出上限”预扣，结束后优先用中转站返回的真实 `usage` 结算，没有 usage 时用本地估算兜底；无输出（失败/空回复/首字前断开）自动退款。
- 中转站：`chat_completions` 默认带 `stream_options.include_usage=true`；不支持该参数时设 `EV_LLM_STREAM_USAGE=0`。usage 块在 `finish_reason` 之后到达，已修复原提前返回导致的丢失。
- 生图：每日额度检查后扣费，生成或保存失败自动退款；
- GEO：估算返回 `credits`/`balance`/`enough`（已修正漏乘引擎数）；开始体检预扣、结束按成功条数结算，失败/取消部分退回。
- 未扣费：数字人文稿/翻译、WhatsApp 自动回复/翻译、运营 AI、文案工坊、GEO 问题库自动生成（后续可接入同一套 `charge/adjust`）。
- 前端零改动；验证：118 个测试通过（`tests/test_billing.py` 13 个 + 路由对话/生图用例）；Playwright 页面验收通过（对话按 usage 结算/生图/GEO/余额不足提示），产物在 `验收-本地基线-2026-11-19/算力扣费/`（未跟踪）。
- 已知限制：中转站不返回 usage 时走本地估算（可能少量偏差）；GEO 进程被杀可能留下未结算预扣（无启动对账）；新租户仍默认 100 算力；消费账单金额是折算价，不等于各算力包折扣率；真实中转站联调需有效 key（本机旧 key 返回 401）。

### 4.20 GEO 品牌事实与竞品（`f700568`）

- 文件：`evcommander/geo_brand.py`、`geo.py`、`geo_runs.py`、`serve.py`
- 新增 `geo_facts` / `geo_competitors`（按租户隔离），品牌页 `/geo/brand` 的接口全部真实落库：
  - 事实：列表、手工新增/修改、批量审核（确认/否决/删除）、按知识库 AI 整理（待确认）、项目最多 100 条；
  - 竞品：列表、批量新增（最多 10 个）、修改、删除、AI 推荐（过滤自己和已有竞品）；
  - 声量份额：按 30 天窗口内成功回答文本统计品牌/竞品出现条数与占比，`others` 为引号名称/英文品牌词启发式提取（至少 2 条回答）。
- 项目 `counts.competitors` / `counts.facts_confirmed` 改为真实计数；项目对象补 `can_edit`。
- 修复前端无 body POST：`resume`、`facts/extract`、`competitors/suggest`、`runs/cancel|retry` 放行空 body，其余 POST/PUT 仍要求 JSON（`geo.empty_post`）。
- 内容工坊/工单改为明确「暂未开放」；工商/门店核对明确「需要配置企业数据源」，不再假成功。
- 测试：新增 14 个 `tests/test_geo_brand.py` + 1 个路由用例；全量 **133 个通过**。
- 页面验收：`/geo/brand` 整理事实、手工事实、加竞品、推荐、2 条模拟体检后的声量份额均正常；控制台仅既有 `power-prices` 503。
- 限制：事实整理/竞品推荐暂不扣算力；声量份额基于模拟回答的字符串匹配（别名可能误命中）；`others` 中文未加引号的品牌名可能漏掉；内容/发布/诊断仍未实现（关键词矩阵已完成，见 4.21）。
- 详见运行记录第三十节。

### 4.21 GEO 关键词矩阵（`072f61b`）

- 文件：`evcommander/geo_matrix.py`、`geo.py`、`geo_runs.py`
- 五层词库：找商家（entity）、找品类（category）、按场景（scene）、口语说法（slang）、具体疑问（question）；三档 S 强需求 / A 有意向 / B 了解阶段。
- 词根分组：地域、行业叫法、商家类型、场景、口语说法；自动组合规则：
  - 地域 + 行业叫法、行业叫法 + 商家类型、三者组合 → 找商家；
  - 行业叫法 + 场景、地域 + 场景、场景单词 → 按场景；
  - 行业叫法单词 → 找品类；口语说法单词 → 口语说法；
  - 组合词档位取最弱一档；最多 300 个词，按组限制词根数。
- 前端 `/geo/questions?tab=matrix` 契约全部接通：`kw-matrix` 读/保存、`draft`（AI 起草，无 body POST）、`terms` 增查、`terms/batch` 改档/删除、`convert` AI 改写、`questions` 加入问题库、`doc` 导出文档；「写一篇」仍明确「内容工坊暂未开放」。
- 保存词根保留手工词、改档词、已转问题的词；删除规则组合的过期词。问题删除时通过 `geo_questions.term_id` 把词条改回未转。
- `/projects/{id}/layers` 由空数据改为真实层次统计（问题数、有效回答、被提到比例、判断）；AI 体检页「按需求层次」可用；`top1_rate` 暂不计算，显示「—」。
- 计费：AI 起草/改写暂不扣算力（与事实整理、问题生成一致）；前端「按用量扣少量积分」文案是后续接入占位。
- 测试：新增 20 个 `tests/test_geo_matrix.py` + 1 个路由用例；全量 **154 个通过**。
- 页面验收：起草 13 个词、编辑词根、加词、转问题、加入问题库、导出 Word（10,274 字节）、体检页层次统计均正常，控制台 0 错误；产物在 `验收-本地基线-2026-11-19/geo-matrix/`（未跟踪）。
- 限制：已发内容覆盖固定 0（内容工坊未实现）；层次判断阈值是启发式；旧问题无 `term_id`，删除后不回退旧词条。「分享给客户」已在 4.22 接通。
- 详见运行记录第三十一节。

### 4.22 GEO 报告详情与公开分享（`76d7303`）

- 文件：`evcommander/geo_report.py`、`geo.py`、`geo_matrix.py`、`geo_runs.py`、`serve.py`
- 周报/月报：按自然周（周一起）/自然月聚合已完成体检的回答，`offset` 0–24 往前翻；指标包括被提到比例、排第一比例、同行占比、信息准确率（暂 null → 前端「—」）、与上一期差值；维度包括本期每天、各家 AI、每个问题、按需求层次、按问题类型、被竞品占住的问题、主要发现、回答原文摘录（品牌/竞品高亮）。
- 分享：`geo_report_shares` 存随机 token + 快照 JSON + 到期 + 打开次数 + 撤销状态；支持 week/month/matrix，matrix 快照词库矩阵 doc（`kind:"kw_matrix"`）；`diag` 仍返回未开放。
- 公开访问：`GET /api/public/geo/report/{token}`（同时兼容 `/agent-api/api/public/...`）无需登录；过期/撤销返回 `{ok:false}`；公开快照不变。
- 邀请模式：`/geo-share/<token>`、`/assets/`、`/fonts/`、`/api/brand/by-domain|config|current` 无需登录即可访问（公开分享页所需，非敏感静态资源），其余仍需登录。
- 诊断占位：本章节完成时 `diagnoses/estimate` 返回 blockers「诊断方案暂未开放。」；该占位已由 4.23 的真实诊断/告警替换。
- 测试：新增 11 个 `tests/test_geo_report.py` + 1 个路由用例；全量 **166 个通过**。
- 页面验收：周报全部区块、往前一期、分享链接、公开分享页（无登录）、打印页、词库矩阵公开页、撤销后失效、邀请模式公开页 0 控制台错误；产物在 `验收-本地基线-2026-11-19/geo-report/`（未跟踪）。
- 限制：准确率/引用来源/发布/提醒仍为空（等诊断/内容模块）；排位与同行占比按文本首次出现和已知竞品别名启发式计算；分享 token 明文存库；撤销记录保留在列表；报告按 UTC 周/月切分。
- 真实 `.local/commander.sqlite3` 冒烟时仅新增空的 `geo_report_shares` 表，业务数据未变。
- 详见运行记录第三十二节。

### 4.23 GEO 诊断方案与告警（`356d6c3`）

- 文件：`evcommander/geo_diagnosis.py`、`geo_report.py`、`geo_brand.py`、`geo_runs.py`、`geo.py`
- 诊断：以一次已完成体检为基线，生成 DiagnosisBody 完整数据（核心指标、定位/买家/比较维度、问题意图、现状、AI 推荐、层次、目标参考、12 周计划、信号、摘录、边界、总结）；数字由回答计算，分析段由一次文本模型调用写，失败时数据照常、AI 部分退款。
- 接口：`/projects/{id}/diagnoses`（列表/启动）、`diagnoses/estimate?run_id=`、`/api/geo/diagnoses/{id}`；同一体检已有 running 复用，已完成且 `force=false` 返回 `{ok:false,code:"exists"}`。
- 计费：按输入估算 + 1600 输出上限预扣，usage/估算结算；AI 失败全额退；关闭扣费/试用模式 credits 记 0。
- 分享/打印：`report-shares` 支持 `period:"diag"` + `diagnosis_id`，快照带 `kind:"diagnosis"`；公开页/打印页直接渲染。
- 告警：`geo_alerts`；生成诊断时检查被提到比例下降（≥20 点，≥40 严重）、竞品上升（≥20 点）、体检失败率（≥20%）；`GET /projects/{id}/alerts?status=`、`PUT /api/geo/alerts/{id}`；工作台提醒面板可处理。
- 工作台 `/projects/{id}/overview` 改为真实数据：最近体检 current/delta、近 30 天趋势、各家 AI、上次体检、计数、待处理告警；内容/引用/待办仍空。
- 声量份额补 `answers` 和 `brands`（自己/竞品/其他品牌 + 份额），工作台同行对比图可用；品牌页原字段不变。
- 测试：新增 9 个 `tests/test_geo_diagnosis.py` + 路由用例 + 总览/份额断言；全量 **176 个通过**。
- 页面验收：生成诊断（十章全渲染）、分享公开页、工作台 17%/-83 点/同行图/2 条告警、抽屉标记已处理，控制台 0 错误；产物在 `验收-本地基线-2026-11-19/geo-diagnosis/`（未跟踪）。
- 限制：告警只在生成诊断时检查；只做三类告警；引用来源已由 4.26（`1f291dd`）接通，信息准确率仍空；目标不足时不编造；诊断线程单进程，重启后 running 会卡住；工作台待办仍空（内容计数已在 4.24 接入，引用计数已在 4.26 接入）。
- 详见运行记录第三十三节。

### 4.24 GEO 内容工坊与手工发布（`eb90631`）

- 文件：`evcommander/geo_content.py`、`geo.py`、`geo_matrix.py`、`geo_report.py`
- 内容：`geo_contents` 存标题/正文/摘要/写作要求/问题/类型/来源/平台/语言/状态/版本/资料引用/评分/质检/平台版本；`content-meta` 提供文章类型、平台、渠道类型；`opportunities` 从最近一次体检和词库缺口生成选题。
- 接口：`/projects/{id}/contents`（GET/POST）、`/api/geo/contents/{id}`（GET/PUT/DELETE）、`generate/score/check`（无 body POST）、`approve`、`apply-suggestion`、`adapt`、`variants/{platform}`、`tickets`；`/projects/{id}/opportunities|publications|tickets`、`/api/geo/tickets/{id}`、`/api/geo/publications/{id}/url`。
- AI：写初稿（品牌事实/知识库/写作要求，Markdown，[F1]/[K1] 资料号）与平台改写均为后台线程 + 单次模型调用；失败只写 `error`、不产生账单；暂不扣算力。
- 质检：规则版关键说法核对 + 广告法极限词；decision pass/review/block，gate 与正文签名绑定；人工放行需理由并记录；采纳建议自动作废质检/评分。
- 发布：`geo_tickets` 生成可复制排版草稿与平台入口，贴回链接后写 `geo_publications`、内容置为已发布；自动渠道/自有账号/媒体/知识页仍明确未开放（`channels` 空、`push` 返回未接通）。
- 覆盖：词库矩阵 `docs`/`covered`/`strong_covered` 按已发布正文计算；报告 `publishing` 按已发布记录统计；工作台 `content_counts` 接入内容/发布/工单数。
- 词库矩阵「写一篇」现在创建真实内容草稿（`source_kind=matrix`）。
- 测试：新增 13 个 `tests/test_geo_content.py` + 路由用例；全量 **190 个通过**。
- 页面验收：新建→AI 初稿→质检（5 条关键说法）→人工放行→知乎改写（相似度 39%）→自己去发→贴回链接→发布记录→报告「本期发布」；控制台仅既有 `power-prices` 503 和缺失的 `/assets/platforms/zhihu.svg` 404。
- 限制：写作/改写不扣算力；质检/评分是规则版；平台改写一次调用；引用解析已由 4.26（`1f291dd`）接通；WordPress/自建官网自动推送已由 4.29（`89c65d5`）接通，公众号/媒体仍只用「自己去发」或手工工单；已发布内容不可改/删。
- 详见运行记录第三十四节。

### 4.25 GEO 品牌知识页（`ad906fb`）

- 文件：`evcommander/geo_hub.py`、`geo.py`、`serve.py`、`customers.py`
- 站点：每个项目一个托管品牌知识页；标题/介绍/留言开关/对外开放；默认标题取品牌名、介绍取项目描述，默认对外开放、留言关闭（`geo_hubs`）。
- 页面：只能从「质检通过或人工放行」的内容上页，保存上页时快照（`geo_hub_pages`）；一篇文章一页，每项目默认每天最多 20 篇（`EV_HUB_DAILY_LIMIT`）；支持上下线、换栏目（不改页面地址）；正文改过并重新质检后「同步」最新快照。
- 门禁错误码：`gate_required`（未质检）/`gate_stale`（改过未重检）/`gate_review`（未放行），前端弹「要先重新质检 → 去质检」。
- 公开站点（无需登录）：`/geo-hub/{project_id}/` 预览（始终 noindex）、`/p/{page_id}`、`/llms.txt`、`/robots.txt`、`/sitemap.xml`；服务端渲染、Markdown 安全渲染、JSON-LD；品牌事实（已确认）展示在首页与 llms.txt；站点关闭/页面下线 404。
- 访问统计：`geo_hub_views` 按 User-Agent 识别常见 AI 爬虫、按 Referer 识别 AI 来源；`hub/stats?days=` 返回 bot_total/ai_visits/other_visits/leads/bots/sources。
- 留言：`POST /geo-hub/{id}/lead` 写入客户中心，来源 `hub_lead`「品牌知识页留资」；同联系方式去重、蜜罐、同项目+IP 10 分钟 5 次；`geo_hub_leads` 按项目记账。
- 域名：`PUT .../hub/domain` 保存并生成 CNAME+TXT 记录；`POST .../hub/domain/check` 用 DNS-over-HTTPS（`EV_HUB_DNS_DOH`）+ `https://{domain}/_ev-hub/verify` 实际核对，状态 pending_dns/pending_cert/active/failed；证书与域名路由由部署环境完成。
- 发布记录：页面首次上线写 `geo_publications(channel_kind='hub')`，下线置 offline；内容本身不改成 published，保持可编辑以支持「改稿→重检→同步」；效果报告「本期发布」按发布记录统计。
- `serve.py` 在登录/邀请门禁前放行公开品牌页和留言；GEO 写接口把查询参数 `origin` 透传；带 `code` 的 GEO 错误按 `{"detail":{"message","code"}}` 返回。
- 计费：全部为本地逻辑/公开访问，不调用文本模型，不扣算力。
- 测试：新增 `tests/test_geo_hub.py` 17 个 + `tests/test_routes.py` 1 个；全量 **208 个通过**。
- 页面验收：0 篇→上页 2 篇→设置→公开页/详情/留言→统计（GPTBot/ChatGPT）→下线 404→上线→换栏目→改稿同步→质检失效提示→绑定域名显示 CNAME/TXT；非预期控制台错误 0。
- 限制：域名证书/反向代理需部署；统计按 UA/Referer 启发式且不去重；一篇文章只能一页；公开站单语言；留言限频为单进程内存。引用解析已于 `1f291dd` 接通（见 4.26），品牌知识页「被 AI 引用」现在显示真实计数。
- 详见运行记录第三十五节。

### 4.26 GEO 引用解析与来源统计（`1f291dd`）

- 文件：`evcommander/geo_sources.py`、`geo_report.py`、`geo_content.py`、`geo_hub.py`、`geo.py`
- 口径：只解析成功回答正文里明确出现的 `http(s)` 链接（Markdown 链接 + 裸 URL），不猜裸域名；同一回答同一 URL 只算一次；同一回答引用同一网站多个页面时「回答数」只算 1 次，「引用条数」按链接数。
- 来源类型：官网（项目/竞品 website，含子域名）、百科、媒体、社区、电商、点评、社交、政府/学术、其他；按域名后缀启发式。
- `GET /api/geo/projects/{id}/sources?days&page&page_size&source_type`：有效回答数、类型统计（`types[].answers`）、网站列表（`domain/source_type/source_name/answers/count/share/samples`）、分页/筛选。
- `GET /api/geo/projects/{id}/gaps`：最近一次完成体检里「推荐了竞品、一次没提你」的问题，含竞品及回答数、涉及 AI；来源页「被竞品占住的问题」可用。
- 周报/月报：`report.sources.domains` 真实；`report.cited` 为 `{pubs,answers,questions,new_pubs,items:[{title,platform_name,answers,new}]}`；无发布记录时 `cited=null`。
- 工作台：`counts.cited_pubs` 与 `cited:[{title,content_title,platform_name,cited_count,last_cited_at}]` 真实（最近 5 篇已发布）。
- 发布记录：`geo_publications.cited_count` 按「同域名+同路径，忽略协议/端口/查询/末尾斜杠」匹配全部历史成功回答，读取发布列表/内容详情/知识页列表时刷新；知识页域名生效/清除时同步 hub 发布记录 URL，使其可被匹配。
- 诊断方案：`scope.citations/unique_domains/avg_citations`、`sources.total/types/domains/own/competitor`、各 AI 的 `citations/domains` 与主要引用来源、示例回答引用数均为真实；边界说明注明口径。
- `POST /api/geo/projects/{id}/cited-pages/analyze`：明确「被引用页面拆解…暂未开放」，不扣算力。
- 计费：全部本地字符串处理，不扣算力；解析按请求实时扫描回答，不新增表、不缓存（回答量小）。
- 测试：新增 11 个 `tests/test_geo_sources.py` + 1 个 HTTP 用例；全量 **220 个通过**。
- 页面验收：来源页类型图/6 个域名/被竞品占住的问题、周报引用网站+被引用文章、工作台 2 篇被引用、发布记录与知识页引用计数均正常；控制台仅有意点击拆解的 400 与既有 zhihu.svg 404。
- 限制：读不到 AI 平台内部引用标记/脚注/搜索来源面板；分类和发布匹配是启发式；诊断 `sources.ranking` 仍为零结构、准确率仍 null。「拆解被引用页面」已于 4.28（`cce96c2`）接通（规则版、免费、6 小时缓存）。
- 详见运行记录第三十六节。

### 4.27 GEO 官网体检与 AI 可读性生成器（`299cf91`）

- 文件：`evcommander/geo_site.py`、`geo.py`
- 官网体检：只能体检项目官网站点下的地址（含子域名）；不执行 JS，抓目标页 + robots.txt + llms.txt + sitemap.xml；私网/回环拒绝、手动重定向并逐跳校验、10 秒超时、约 900 KB 上限；网络抓取不在数据库写事务内。
- 14 项检查：HTTPS、AI 爬虫访问（19 个爬虫，按 robots 最长匹配，区分搜索/用户/搜索索引/训练）、meta noindex、llms.txt、网站地图、标题、描述、h1、正文可读字数、内容结构、JSON-LD、canonical、viewport、lang；每项含状态/说明/影响/修改建议，0–100 分 + 通过/建议/提示/要修计数 + 爬虫逐个放行/挡住。
- 记录：`geo_site_audits`、同项目+URL 10 分钟复用、默认每天 20 次（`EV_SITE_AUDIT_DAILY_LIMIT`）、超过 10 分钟 running 读取时标记失败。
- 接口：`POST /api/geo/projects/{id}/site-audits {url}`、`GET .../site-audits?page&page_size`、`GET /api/geo/site-audits/{id}`。
- 生成代码（不扣算力）：`POST /api/geo/projects/{id}/generators/robots|llms|schema|entity`；robots 支持训练爬虫放行/不放行 + Sitemap；llms 用品牌事实/官网；schema 支持 organization/localbusiness/product/faq/article（article 需 content_id）；entity 支持 directories（国内/海外平台清单）、gbp 逐项清单、wikidata QuickStatements 草稿。结果带 `missing/notice/items/rows` 供前端渲染。
- 配置：`EV_SITE_AUDIT_DAILY_LIMIT`（默认 20）；`EV_SITE_AUDIT_ALLOW_PRIVATE=1` 仅本地验收/测试用，生产不要开。
- 测试：新增 11 个 `tests/test_geo_site.py` + 1 个 HTTP 用例；全量 **232 个通过**。
- 页面验收：首页 93 分（仅 HTTP 扣分）、`/bad` 53 分（HTTPS/noindex/正文 3 项要修）、展开 19 个爬虫；llms/robots/schema 生成正常；控制台仅既有 power-prices 503。
- 限制：只做了「官网体检」「生成代码」两个页签；beacon 访问统计、通知搜索引擎、AI 来访记录、导入搜索平台报表、商品清单未接；体检不执行 JS、只抓一页；robots 解析简化；爬虫清单内置 1 版；sitemap 只看第一个；生成器是草稿、不写回官网、不校验 Schema；平台状态固定「待建」。
- 详见运行记录第三十七节。

### 4.28 GEO 被引用页面拆解（`cce96c2`）

- 文件：`evcommander/geo_sources.py`、`tests/test_geo_sources.py`、`tests/test_routes.py`
- 接口：`POST /api/geo/projects/{id}/cited-pages/analyze {urls,days}`；urls 为空时自动取所选周期引用最多的前 5 个样本 URL；最多 5 页。
- 抓取：公网 http(s)；私网/回环/链路本地/保留地址拒绝；重定向逐跳校验；单页 8 秒超时、约 600 KB；UA `EVCommander-CitedPages/1.0`；网络抓取在数据库写事务之外；单页失败只标记该页。`EV_CITED_ALLOW_PRIVATE=1` 仅本地验收。
- 分析（规则版、免费、不调用模型）：标题/h1–h3/列表表格/字数/具体数字/更新时间/作者/JSON-LD/FAQ/外链；每页 features/why/headings/genre/source_name/answers；汇总共同写法与建议。
- 缓存：`geo_cited_analyses` 按「项目+URL 集合+days」缓存 6 小时；命中 `cached:true`、`credits:0`；全部页面失败不写缓存。
- 返回：`{ok,cached,credits,patterns,advice,pages[{url,title,source_name,genre,domain,answers,ok,features,why,headings,error}]}`。
- 顺带修复：引用 URL 归一化保留非默认端口（发布匹配 key 同步带端口）。
- 测试：新增 4 个拆解用例、移除「未开放」用例；全量 **235 个通过**。
- 页面验收：来源页 3 个样本 → 拆解显示 3/3 标题直接、2/3 小标题/列表/数字/更新时间/作者、页面特征 chips；再次拆解显示 6 小时缓存提示；0 控制台错误。
- 限制：规则版不做语义/模型判断；不执行 JS、不支持 PDF/Word；共同写法至少 2 页/过半；缓存 6 小时不看内容变化；credits 固定 0（前端「按用量扣积分」文案仍是占位）；生产不要开 `EV_CITED_ALLOW_PRIVATE`。
- 详见运行记录第三十八节。

### 4.29 GEO 发布渠道：WordPress / Webhook（`89c65d5`）

- 文件：`evcommander/geo_channel.py`、`geo.py`、`geo_content.py`
- provider：`wordpress`（站点地址/用户名/应用密码/发布状态 publish|draft）、`webhook`（接收地址/可选 Bearer 令牌，POST `{title,content,summary}`，返回 `url`/`link`）；`content-meta.channel_kinds` 改为带 `fields` 的 provider 定义；公众号/服务号、Shopify/Webflow 未接。
- 接入设置：`GET/POST /api/geo/channel-configs`、`PUT/DELETE /api/geo/channel-configs/{id}`、`POST .../{id}/test`（无 body，白名单）；`geo_channel_configs` 按租户；密钥只存库不下发，编辑留空保持原值；`last_test`/`last_error`。
- 推送：`POST /api/geo/contents/{id}/push {channel_id}`；内容必须可发布、渠道启用；WordPress REST `/wp-json/wp/v2/posts`（Basic 应用密码）；Webhook JSON；成功写 `geo_publications`（`channel_kind='channel'`），直接发布改内容状态为 published，草稿/无链接保持可编辑；失败 `{ok_push:false}` 并写 `last_error`，无发布记录；网络请求不在写事务内。
- 安全：http(s)、拒绝内网/回环、逐跳重定向校验、15 秒超时、约 500 KB；`EV_CHANNEL_ALLOW_PRIVATE=1` 仅本地验收。
- 不扣算力。
- 测试：新增 10 个 `tests/test_geo_channel.py` + 1 个 HTTP 用例；`test_geo_content` 的 channel_kinds 断言更新；全量 **246 个通过**。
- 页面验收：接入设置添加 WordPress → 自动测试成功；待发内容「发布」→「官网与公众号」→ 推送 → 发布记录显示渠道/链接/已发布，内容状态 published；0 控制台错误。
- 限制：无公众号/Shopify/Webflow；WordPress 需 Application Passwords，未处理封面/分类/定时；Webhook 为自定义契约；密钥明文存 SQLite；失败不重试、无下线/删除；一次一个渠道；同步请求最长约 15 秒。
- 详见运行记录第三十九节。

### 4.30 GEO 发布渠道增强：后台队列、重试、封面、分类标签、下线（`02f5200`）

- 文件：`evcommander/geo_channel.py`、`geo_content.py`、`serve.py`
- 队列：`geo_push_jobs`；`POST .../push` 校验后入队，发布记录先记 `queued`，worker 串行处理；状态 `queued → running → published|draft_saved|failed`。
  - 默认最多等 3 秒拿首屏结果（`EV_CHANNEL_PUSH_WAIT`）；`EV_CHANNEL_SYNC_PUSH=1` 请求内同步执行（测试/本地验收用）。
  - 网络错误/HTTP 5xx/408/429 可重试，最多 3 次、退避 5/10 秒；其它 4xx 与配置/内容问题立即失败；失败写渠道 `last_error` 与发布记录 `error`。
  - `recover_push_jobs()` 启动恢复（`serve.py` 调用）；同一内容+渠道已有排队任务时不重复建。
- 封面：前端推送传 `cover_url`（材料库 `/material-media/<key>` 的绝对 URL）；后端只取材料库文件（校验租户/图片类型），WordPress 先传 `/wp-json/wp/v2/media` 再写 `featured_media`；Webhook 附带 `cover_url`。
- 分类/标签：WordPress provider 新增「默认分类」「默认标签」字段；按名称查询 `/wp-json/wp/v2/categories|tags`，不存在自动创建；失败整次推送失败。
- 下线：`DELETE /api/geo/publications/{id}`；WordPress 有 `remote_id` 时调 `DELETE /wp-json/wp/v2/posts/{id}`（回收站）并置 `offline`；Webhook/历史记录只本地标记；queued 可取消；内容无其它有效发布记录时回到 `ready`。
- `geo_publications` 新增 `channel_id`、`remote_id` 列（旧库自动补）。
- 前端零改动，未动 `mirror/`、i18n、`website/`。不扣算力。
- 测试：新增 17 个用例（队列/重试/恢复/封面/分类标签/下线），全量 **263 个通过**。
- 页面验收：接入设置新字段 → 推送成功（categories/tags 生效）→ 失败模式「排队中」→「失败」 → API 带封面推送（featured_media）→ API 下线（记录已下线、内容回到可发布）；控制台 0 错误。
- 限制：前端发布记录无轮询（需手动刷新看异步状态）；封面按钮只在公众号（未接）显示，WordPress/Webhook 封面走 API；分类标签仅 WordPress；下线无前端入口；队列为单进程线程；历史记录无 `remote_id` 只能本地标记。
- 详见运行记录第四十节。

### 4.31 GEO 官网工具「AI 来访记录」（`26e955b`）

- 文件：`evcommander/geo_site.py`、`serve.py`
- 上传：`POST /api/geo/projects/{id}/crawl-logs`（multipart，字段 `file`），支持 Nginx/Apache combined/common、每行一条 JSON、`.gz`；单文件 30 MB、解压后 120 MB、最多 100 万行；原始日志不落库，只存按天汇总；同项目同文件（sha256）不重复统计；没有 AI 爬虫也会记录上传（空状态提示用）。
- 识别：复用 `geo_site.BOTS` 的 19 个 AI/搜索爬虫（UA 子串、最长名称优先）；只统计识别出的爬虫。
- 表：`geo_crawl_logs`（文件/时间段/行数/认出数）、`geo_crawl_daily`（日期+爬虫+用途+hits+errors+top paths）、`geo_crawl_errors`（日期+路径+状态+爬虫+次数），按租户。
- 接口：`GET .../crawl-logs?page&page_size`；`GET .../crawl-stats?days=7|30|90` → `{total,start,end,uploads,note,bots,purposes,series,error_paths}`；`DELETE /api/geo/crawl-logs/{id}`。
- `serve.py` 新增 `write_geo_upload`（multipart 专用路由，在 GEO JSON 写之前匹配）。
- 不扣算力；前端零改动（`/geo/site` 的「AI 来访记录」页签已有上传/统计/删除）。
- 测试：新增 7 个 `CrawlLogTests` + 1 个 HTTP 用例；全量 **271 个通过**。
- 页面验收：空状态 → 上传 6 行日志 → 4 个 AI 爬虫与 `/missing` 报错页 → 近 7 天 → 删除；控制台仅既有 `power-prices` 503。
- 限制：格式/UA 识别是启发式（伪造 UA 会算入）；日期按日志文本、不换算时区；超出 100 万行的部分不统计；其他官网工具页签（beacon 访问统计、报表导入、收录提交、商品清单）未接。
- 详见运行记录第四十一节。

### 4.32 GEO 官网工具「AI 带来的访问」（`1673bd5`）

- 文件：`evcommander/geo_site.py`、`serve.py`、`customers.py`
- 统计代码：`GET /api/geo/projects/{id}/beacon?origin=` → `{ok,site,sources}`；`site.snippet` 是 `<script async src="{origin}/geo-beacon.js?k={token}"></script>`；每项目随机 token，按租户；`sources` 为 15 个可识别 AI 来源。
- 配置：`PUT .../beacon {leads_enabled?,reset_code?}` → `{ok,site}`；`reset_code` 换 token（旧代码立即失效），`leads_enabled` 控制表单留资进线索池。
- 公开端点（登录门禁前放行，CORS `*`）：`GET /geo-beacon.js?k=`（脚本）、`POST /geo-beacon/hit`（页面访问）、`POST /geo-beacon/lead`（表单电话）。
  - 脚本读取 `document.referrer` 识别来源并 `sendBeacon`；监听表单 `submit` 抓 `type=tel` 或名称含 phone/tel/手机/电话的输入；
  - 不写 Cookie、不存 IP/完整 referrer；来源 key 白名单校验；hit 每天每项目上限 5000；lead 仅在开关开启时接收、电话格式校验、同电话去重。
- 统计：`GET .../beacon/stats?days=7|30|90` → `{total_visits,ai_visits,ai_leads,start,end,series,sources,pages}`；只把 AI 来源计入 ai_visits/sources/pages。
- 表：`geo_beacons`、`geo_beacon_hits`；客户中心来源显示 `beacon_lead → 官网留资`。
- 不扣算力；前端零改动。
- 测试：新增 7 个 `BeaconTests` + 1 个 HTTP 用例；全量 **279 个通过**。
- 页面验收：统计代码 → 模拟上报 3 AI + 1 直访 + 1 留资 → 统计/来源/页面 → 近 7 天 → 开关 → 换代码；控制台仅既有 503。
- 限制：需客户网站安装；只按 referrer 域名识别（微信/App/隐私模式可能为空）；只识别 15 个 AI 来源；SPA 路由切换不重复上报；留资只抓标准电话字段。
- 详见运行记录第四十二节。

### 4.33 GEO 官网工具「导入搜索平台报表」（`8862796`）

- 文件：`evcommander/geo_site.py`、`serve.py`
- 上传 `POST /api/geo/projects/{id}/imports`（multipart：`file`、可选 `source`/`period_start`/`period_end`）：CSV/TSV/TXT、ZIP（最多 8 个 CSV）；20 MB；UTF-8（含 BOM）；逗号/制表符自动。
- 来源：`bing_ai`（必应 AI 表现/Copilot 引用）、`bing`、`gsc`；自动识别：有 Citations → bing_ai，有 Top pages/Top queries → gsc，否则 bing。
- 列识别（中英文）：点击/展示/点击率/平均排名/引用；行键日期→按天、搜索词→按搜索词、页面→按页面；页面报表按 `geo_publications` 已发布 URL 标 `ours`。
- 接口：`GET .../imports?page&page_size&source` → `{items,total,sources}`；`GET /api/geo/imports/{id}` → `{import}`（metrics/metric_names/totals/items）；`DELETE /api/geo/imports/{id}`；表 `geo_imports`。
- `serve.py` 的 GEO 上传处理器改为通用（`upload_limit`/`handle_upload`），同时服务访问日志与报表。
- 不扣算力；前端零改动。
- 测试：新增 7 个 `ImportTests` + 1 个 HTTP 用例；全量 **287 个通过**。
- 页面验收：GSC 页面 CSV → 详情（点击 200/平均排名）→ 必应 AI（引用 5）→ 中文按天 → 来源筛选 → 删除；控制台仅既有 503。
- 限制：不支持 xlsx/PDF；列名别名有限；明细只留前 100 行；自动识别是启发式；CTR/排名合计口径与平台可能略有差异；重复导入不去重。
- 详见运行记录第四十三节。

### 4.34 GEO 官网工具「商品清单」与 Merchant Center 数据源（`7398374`）

- 文件：`evcommander/geo_site.py`、`serve.py`
- 商品 CRUD：`GET /api/geo/projects/{id}/products?keyword&issues&page&page_size` → `{items,total,count,limit,host,currency,availability,conditions,checklist}`；`POST .../products`、`PUT /api/geo/products/{id}`、`POST .../products/delete {ids}`；`sku/title` 必填、链接必须在项目官网域名、上限 200（`EV_PRODUCTS_LIMIT`）；item 带动态 `issues`。
- CSV 导入：`POST .../products/import`（multipart `file`），表头至少 `id`+`title`，认 Merchant Center 与中文表头；按编号新增/更新；非法行跳过并提示。
- 数据源：`GET .../products/feed?format=xml|tsv` → `{text,file}`；`GET .../products/feed-url?origin&reset=1` → `{url,last_fetch_at}`；公开 `GET /geo-products/{token}.xml|tsv`（登录门禁前放行、更新最近抓取、`X-Robots-Tag: noindex`）。
- 表：`geo_products`、`geo_product_feeds`；不扣算力；前端零改动（仅 `market=global` 显示）。
- 测试：新增 6 个 `ProductTests` + 1 个 HTTP 用例；全量 **294 个通过**。
- 页面验收：添加商品 → CSV 导入 → 数据源地址 → 下载 XML（g:id/g:price）→ 只看有问题 → 全选删除；控制台仅既有 503。
- 限制：只支持 CSV/TSV；表头别名有限；第三方链接被拒；200 个上限；feed 只读、不校验 Merchant 类目合规；无 Merchant API 对接。
- 详见运行记录第四十四节。

### 4.35 GEO 官网工具「通知搜索引擎」（`1ed7711`）

- 文件：`evcommander/geo_site.py`、`geo.py`
- 状态：`GET /api/geo/projects/{id}/index-submit` → `{ok,state,candidates}`；`state` 含 `host/note/indexnow/baidu/history`；`candidates` 为发布记录里官网域名文章（默认勾选前 20）。
- IndexNow：每项目随机密钥；`key_file/key_url` 给前端展示；`POST .../index-submit/verify`（无 body）抓取 `官网/<key>.txt` 验证；`POST .../index-submit {target:"indexnow",urls}` 只提交官网域名地址，未验证/超每日上限（`EV_INDEXNOW_DAILY` 默认 100）拒绝；调用 `EV_INDEXNOW_BASE`（默认 `https://api.indexnow.org`）。
- 百度：`PUT .../index-submit/baidu {site,token}` 保存/留空保持/站点空清除，令牌不回传；`POST .../index-submit {target:"baidu",urls}` 调 `EV_BAIDU_INDEX_BASE`（默认 `http://data.zz.baidu.com`），返回 `success/remain`。
- 历史：`geo_index_history` 最近 10 次展示；每次提交写记录；网络请求在写事务外。
- `geo.py`：`index-submit/verify` 加入空 body POST 白名单（前端无 body）。
- 不扣算力；前端零改动。
- 测试：新增 6 个 `IndexTests` + 1 个 HTTP 用例；全量 **301 个通过**。
- 页面验收：未验证 → 验证 → IndexNow 提交 → 百度保存/提交/配额；控制台仅既有 503。
- 限制：真实 IndexNow/百度未联调（本地假服务验收）；Google 无公开即时收录 API；官网须公网可访问（本地验收开 `EV_SITE_AUDIT_ALLOW_PRIVATE=1`）；官网换域名需重新验证。
- 详见运行记录第四十五节。

## 5. 当前本地测试数据

- SQLite：`evcommander/.local/commander.sqlite3`
- 已迁移为多租户结构（演示租户 `demo`，另有 `tenants`、`users`、`sessions` 表）：
  - 1 个 GEO 项目 `TestBrand`
  - 1 条获客线索“测试客户A”（电话 13800000001，来源 map）
  - 6 笔充值订单、7 条账单，余额 1550、累计充值 120
- 算力扣费（`ae59c45` + `110df68`）上线后，本地预览的对话（按 token）/生图/GEO 体检会真实扣演示租户余额并写 `consume` 账单；截至 2026-11-22 的全部验收使用临时库，真实 `.local/commander.sqlite3` 未被改写
  - 2 个本地会话、13 个素材文件、10 张生图作品
- 迁移前备份：`evcommander/.local/commander.sqlite3.pre-tenancy-2026-11-20.bak`（未跟踪，保留本地）。
- 本地预览模式不需要登录，固定使用演示租户；如需模拟多租户，用 `tenant_admin.py` 建租户/账号，再启动 invite 模式。
- WhatsApp 相关表（`wa_accounts`/`wa_threads`/`wa_messages`/`wa_roles`）在首次访问时创建；真实本地库当前没有 WhatsApp 账号（验收用临时库）。
- 多租户第二阶段：真实库下次启动会自动补 `tenants.max_users`（demo=50）并创建 `audit_log`；管理页面只有在设置 `EV_ADMIN_PATH` 时才提供服务。
- WhatsApp 增强：下次启动会创建 `wa_outbox` 并给旧 `wa_messages` 补 `media_id`；媒体文件归档到素材库（`DATA_DIR/material-media`），发件箱 worker 单进程运行。
- 登录安全：下次启动会补 `users.totp_secret/totp_enabled` 并创建 `login_log`；现有账号默认未开启 2FA。
- GEO 关键词矩阵：下次启动会补 `geo_questions.term_id`；`geo_kw_state`/`geo_kw_roots`/`geo_kw_layers`/`geo_kw_terms` 在首次访问矩阵接口时创建。
- GEO 报告分享：`geo_report_shares` 在首次访问报告/分享接口时创建；真实 `.local/commander.sqlite3` 在 2026-11-24 冒烟检查中已创建该空表（业务数据未变：余额 1550、项目 TestBrand、问题等原样），下次正式启动不会重复建表。
- GEO 诊断/告警：`geo_diagnoses`、`geo_alerts` 在首次访问诊断/告警/工作台接口时创建；真实库当前尚未创建，业务数据未变。
- GEO 内容/发布：`geo_contents`、`geo_tickets`、`geo_publications` 在首次访问内容/发布/词库覆盖接口时创建；真实库当前尚未创建，业务数据未变。
- GEO 品牌知识页：`geo_hubs`、`geo_hub_pages`、`geo_hub_views`、`geo_hub_leads` 在首次访问 `/geo/hub` 管理接口或公开页/留言时创建；真实库当前尚未创建（业务数据未变）。品牌知识页公开地址是 `/geo-hub/{project_id}/`；绑定域名需部署环境设置 `EV_HUB_CNAME` 并完成证书/反向代理。
- GEO 引用解析：**不新增表**，按请求从 `geo_answers` 实时解析；`geo_publications.cited_count` 在读取发布列表/内容详情/知识页列表时刷新；真实库无需迁移（当前业务数据未变）。
- GEO 官网体检：`geo_site_audits` 在首次访问 `/geo/site` 体检接口时创建；真实库当前尚未创建（业务数据未变）。历史体检保留在表里；同地址 10 分钟内复用。
- GEO 被引用页面拆解：`geo_cited_analyses` 在首次拆解时创建（缓存 6 小时）；真实库当前尚未创建，业务数据未变。
- GEO 发布渠道：`geo_channel_configs` 在首次打开接入设置时创建；真实库在 2026-11-26 测试期间已被误建为空表（另一张误建的 `geo_push_jobs` 也是本模块队列表，均无数据），业务数据未变。渠道配置按租户，密钥只存库不下发。
- GEO 推送队列：`geo_push_jobs` 在首次推送时创建；真实库 2026-11-26 已有空表；`serve.py` 启动时 `recover_push_jobs()` 把 `sending` 改回 `pending` 并启动 worker。
- GEO 官网工具 AI 来访：`geo_crawl_logs`/`geo_crawl_daily`/`geo_crawl_errors` 在首次上传日志时创建；真实库当前尚未创建（业务数据未变）。
- GEO 官网工具 AI 带来的访问：`geo_beacons`/`geo_beacon_hits` 在首次打开该页签或收到上报时创建；真实库当前尚未创建（业务数据未变）。
- GEO 官网工具报表导入：`geo_imports` 在首次导入时创建；真实库当前尚未创建（业务数据未变）。
- GEO 官网工具商品清单：`geo_products`/`geo_product_feeds` 在首次打开商品页签时创建；真实库当前尚未创建（业务数据未变）。
- GEO 官网工具通知搜索引擎：`geo_index_state`/`geo_index_history` 在首次打开该页签时创建；真实库当前尚未创建（业务数据未变）。
- 管理审计账号/口令：测试口令只存在于临时测试库，不写进文档/提交。
- 需要干净演示时，可以删除 `.local/commander.sqlite3` 后重启（会失去所有测试数据，并重建演示租户）。
- `验收-本地基线-2026-11-19/` 是未跟踪的截图/JSON，保留本地即可。

## 6. 接下来做什么

**决策已于 2026-11-20 拍板，见 `决策单-待确认事项-2026-11-19.md` 顶部「拍板结果」。多租户第一阶段已于 2026-11-20 完成（`5c65706`）。**

**2026-11-21：产品核心功能设计草案已出（`EV指挥官-核心功能设计-2026-11-21.md`）。2026-11-22：用户拍板先做算力扣费，已完成（`ae59c45`，对话改为按 token 见 `110df68`；详见运行记录第二十八、二十九节）；设计文档其余问题仍待拍板。**

**2026-11-23：GEO 品牌事实与竞品已完成（`f700568`，详见运行记录第三十节）。2026-11-24：GEO 关键词矩阵（`072f61b`）、报告详情与公开分享（`76d7303`）、诊断方案与告警（`356d6c3`）、内容工坊与手工发布（`eb90631`）已完成（详见运行记录第三十一至三十四节）。2026-11-25：GEO 品牌知识页与公开站点（`ad906fb`）、引用解析与来源统计（`1f291dd`）、官网体检与生成器（`299cf91`）、被引用页面拆解（`cce96c2`）、发布渠道 WordPress/Webhook（`89c65d5`）已完成（详见运行记录第三十五至三十九节）。2026-11-26：发布渠道增强——后台推送队列、失败重试、封面图、WordPress 分类/标签、下线（`02f5200`）已完成（详见运行记录第四十节）。同日：官网工具「AI 来访记录」——访问日志上传解析、AI 爬虫按天汇总、统计与删除（`26e955b`）已完成（详见运行记录第四十一节）。同日：官网工具「AI 带来的访问」——统计代码、beacon 上报、AI 来源/页面统计与表单留资（`1673bd5`）已完成（详见运行记录第四十二节）。同日：官网工具「导入搜索平台报表」——CSV/TSV/ZIP 导入、来源识别、明细与删除（`8862796`）已完成（详见运行记录第四十三节）。同日：官网工具「商品清单」——商品 CRUD、CSV 导入、Merchant Center 定时抓取数据源（`7398374`）已完成（详见运行记录第四十四节）。同日：官网工具「通知搜索引擎」——IndexNow 密钥验证与提交、百度站点/令牌与提交、提交历史（`1ed7711`）已完成（详见运行记录第四十五节）。**官网工具 7 个页签至此全部完成。**当前不依赖外部条件的工作转向公众号/Shopify 等其它渠道、媒体发稿、官网工具剩余页签或其它模块；WhatsApp 联调、真实支付、数字人视频、GEO 真实采集、平台凭据仍等外部条件/拍板。**

### 6.1 外部条件就绪后：WhatsApp 官方 Cloud API 联调

服务端骨架已完成（`f32e24e`），现在缺 Meta 侧凭据和公网回调：

1. 准备 Meta 商业账号、WhatsApp 号码、App ID/Secret、Phone Number ID、长期 Access Token、验证口令；
2. 用 `python whatsapp_admin.py create --tenant <id> --name <名称> --phone-number-id <id>` 配置账号；
3. 在 Meta 后台配置回调 `https://<域名>/webhook/whatsapp`，订阅 `messages` 字段；
4. `python whatsapp_admin.py probe --id <id>` 验证连通；发真实消息检查收件箱、送达状态、人工接管；
5. 需要模板消息时再补前端/群发流程；**不要**先做网页自动化或设备农场（已拍板先不做）。

### 6.2 之后按顺序

1. **GEO 剩余能力（不依赖外部数据源）**：内容工坊/手工发布（`eb90631`）、品牌知识页/公开站点（`ad906fb`）、引用解析与来源统计（`1f291dd`）、官网体检与生成代码（`299cf91`）、被引用页面拆解（`cce96c2`，规则版）、发布渠道 WordPress/Webhook（`89c65d5`）、发布队列/重试/封面/分类标签/下线（`02f5200`）、AI 来访记录（`26e955b`）、AI 带来的访问（`1673bd5`）、搜索平台报表导入（`8862796`）、商品清单（`7398374`）、通知搜索引擎（`1ed7711`）已完成，**官网工具 7 个页签全部完成**；下一步可做公众号/服务号、Shopify/Webflow 等其它渠道（需平台凭据/审核）、媒体发稿（需媒体渠道）、拆解升级为模型分析（需预算/扣费口径）、品牌知识页多语言/自定义主题（可选）、多租户第三阶段（可选）；真实收录凭据到位后按运行记录第四十五节联调；每完成一组单独验收；
2. **数字人**：视频合成；需要视频/口型供应商和预算，没有供应商前继续文稿/TTS 占位；
3. **GEO 真实采集**：需要中转站支持哪些引擎、是否联网搜索、key/预算；报告按实际能力标注；
4. **多租户第三阶段（可选）**：租户内按用户/部门隔离、业务操作审计、2FA、租户自助续费/账单发票；
5. **设备农场 / PC 助理**：先不做；
6. **支付**：继续 mock，不接真实渠道。

## 7. 前端契约速查

- 支付：`/agent-api/pay/*`、`/agent-api/order/*`、`/agent-api/bill/*`、`/agent-api/card/*`、`/agent-api/asset/*`；响应需要 `{code:"0000",msg:"",data:...}`。
- 获客/CRM：`/agent-api/api/acquisition/*`、`/scrm-api/api/scrm/contacts*`；响应多为直接对象。
- GEO：`/agent-api/api/geo/*`；响应直接 `{ok:true,...}`。品牌档案事实/竞品见 `geo_brand.py`：`/projects/{id}/facts`（`items`/`keys`）、`facts/{id}`、`facts/review`、`facts/extract`、`/competitors`（最多 10 个）、`competitors/{id}`、`competitors/suggest`、`competitors/sov?days=30`（`items`/`others`）；前端这些动作多为无 body POST，空 body 白名单在 `geo.empty_post()`。
- GEO 关键词矩阵见 `geo_matrix.py`：`/projects/{id}/kw-matrix`（GET/PUT，PUT `{roots,layers}`）、`kw-matrix/draft`（AI 起草，无 body POST）、`kw-matrix/terms`（GET/POST）、`terms/batch`（`{ids,action,tier}`）、`convert`（`{ids,locale}`，一次 ≤30）、`questions`（`{items}`）、`kw-matrix/doc`（Word 导出数据）；「写一篇」`layers/{layer}/content` 仍返回「内容工坊暂未开放」。问题列表返回五层 `layers` 和 `layer_name`；`/projects/{id}/layers` 返回体检页层次统计（`rows`/`layered`/`unlayered`）。
- GEO 报告/分享见 `geo_report.py`：`/projects/{id}/reports?period=week|month&offset=`（`report` 含 current/delta/trend/engines/questions/layers/intents/gaps/findings/examples/project 等）、`/projects/{id}/report-shares`（GET 列表 / POST 创建 `{period,offset,days}`，period 支持 week/month/matrix）、`DELETE /api/geo/report-shares/{id}`、公开 `GET /api/public/geo/report/{token}`（无登录，前端也会走 `/agent-api/api/public/...`）。矩阵分享快照带 `kind:"kw_matrix"`；`period:"diag"` 需带 `diagnosis_id`，快照带 `kind:"diagnosis"`。邀请模式下 `/geo-share/<token>`、`/assets/`、`/fonts/`、`/api/brand/*` 公开，其余需登录。
- GEO 诊断/告警见 `geo_diagnosis.py`：`/projects/{id}/diagnoses`（GET 列表/POST `{run_id,force}`）、`/projects/{id}/diagnoses/estimate?run_id=`、`/api/geo/diagnoses/{id}`（`diagnosis.data` 是 DiagnosisBody 结构）、`/projects/{id}/alerts?status=open|handled|ignored&page&page_size`、`PUT /api/geo/alerts/{id}` `{status:"open|handled|ignored"}`。工作台 `GET /projects/{id}/overview` 返回 current/delta/trend/engines/counts/alerts_open/content_counts 等；声量份额 `competitors/sov` 额外返回 `answers` 和 `brands`（kind self/competitor/other + share）。
- GEO 内容/发布见 `geo_content.py`：`/api/geo/content-meta?market=`（genres/targets/channel_kinds/models）、`/projects/{id}/contents`（GET/POST）、`/api/geo/contents/{id}`（GET/PUT/DELETE）、`contents/{id}/generate|score|check`（无 body POST）、`approve`（`{reason}`）、`apply-suggestion`（`{kind,index,text}`）、`adapt`（`{platforms}`）、`variants/{platform}`（PUT `{title,body,tags}` 或 `{remove:true}`）、`contents/{id}/tickets`（`{platform}`）、`/projects/{id}/opportunities|publications|tickets`、`/api/geo/tickets/{id}`（PUT `{status,url}`）、`/api/geo/publications/{id}/url`（PUT `{url}`）。`channels` 为空、`push` 返回未接通自动渠道；媒体接口返回空/未开放。词库矩阵 `kw-matrix/layers/{layer}/content` 现在返回真实草稿 id；矩阵 `docs`/`covered` 按已发布内容计算。
- GEO 品牌知识页见 `geo_hub.py`：`/api/geo/projects/{id}/hub`（GET/PUT `{title,intro,leads_enabled,status}`，GET 可带 `origin` 返回绝对预览地址）、`hub/domain`（PUT `{domain}`）、`hub/domain/check`（无 body POST）、`hub/pages`（GET `?status&page&page_size`/POST `{content_id,section}`）、`hub/candidates`（GET `?page&page_size&focus`）、`hub/stats`（GET `?days=30`）、`/api/geo/hub/pages/{id}`（PUT `{section}` 或 `{live:true|false}`）、`/api/geo/hub/pages/{id}/sync`（无 body POST；失败返回 400 `{"detail":{"message","code":"gate_required|gate_stale|gate_review"}}`）。公开地址：`/geo-hub/{project_id}/`、`/geo-hub/{project_id}/p/{page_id}`、`/geo-hub/{project_id}/llms.txt|robots.txt|sitemap.xml`、`POST /geo-hub/{project_id}/lead`（表单），绑定域名后为 `/`、`/p/{id}`、`/llms.txt`、`/robots.txt`、`/sitemap.xml`、`/lead`、`/_ev-hub/verify`，均在登录门禁前放行；预览地址始终 noindex。发布记录 `channel_kind='hub'`；内容状态不改为 published。
- GEO 引用解析见 `geo_sources.py`：`/projects/{id}/sources?days=7|30|90&page&page_size&source_type` → `{ok,answers,types:[{source_type,name,answers,count}],items:[{domain,source_type,source_name,answers,count,share,samples:[{url,title}]}],total}`；`/projects/{id}/gaps` → `{ok,run,items:[{question_id,question,intent_name,sample_n,competitors:[{name,answers}],engines}]}`；`POST /projects/{id}/cited-pages/analyze {urls,days}` → `{ok,cached,credits,patterns,advice,pages[{url,title,source_name,genre,domain,answers,ok,features,why,headings,error}]}`（规则版，不扣算力，6 小时缓存；urls 为空取前 5 个引用样本）。报告 `sources.domains`、`cited:{pubs,answers,questions,new_pubs,items}`；工作台 `counts.cited_pubs`、`cited:[{title,content_title,platform_name,cited_count,last_cited_at}]`；发布记录 `cited_count` 按「同域名+同路径」匹配全部历史成功回答。口径：只认回答正文里明确出现的 http(s) 链接，同一回答同一 URL 只算一次。
- GEO 官网工具「通知搜索引擎」见 `geo_site.py`：`GET /api/geo/projects/{id}/index-submit` → `{ok,state:{host,note,indexnow:{verified,verified_at,host_changed,key_file,key,key_url,today,daily_limit},baidu:{configured,site,token,today,remain},history},candidates:[{url,title}]}`；`POST .../index-submit/verify`（无 body）→ `{ok,state,message}`；`POST .../index-submit {target:indexnow|baidu,urls}` → `{ok,state,message}`（失败 400）；`PUT .../index-submit/baidu {site,token}` → `{ok,state,message}`。环境变量 `EV_INDEXNOW_BASE`、`EV_BAIDU_INDEX_BASE`、`EV_INDEXNOW_DAILY`（默认 100）。
- GEO 官网工具「商品清单」见 `geo_site.py`：`GET /api/geo/projects/{id}/products?keyword&issues&page&page_size` → `{items,total,count,limit,host,currency,availability,conditions}`；`POST .../products`、`PUT /api/geo/products/{id}`、`POST .../products/delete {ids}`；`POST .../products/import`（multipart `file`，CSV/TSV）→ `{ok,message}`；`GET .../products/feed?format=xml|tsv` → `{text,file}`；`GET .../products/feed-url?origin&reset=1` → `{url,last_fetch_at}`；公开 `GET /geo-products/{token}.xml|tsv`。
- GEO 官网工具「导入搜索平台报表」见 `geo_site.py`：`POST /api/geo/projects/{id}/imports`（multipart `file` + `source`/`period_start`/`period_end`，CSV/TSV/TXT/ZIP，20 MB）→ `{ok,message}`；`GET .../imports?page&page_size&source` → `{items,total,sources}`；`GET /api/geo/imports/{id}` → `{import:{kind,kind_name,metrics,metric_names,totals,items,rows,period_start,period_end,source,source_name}}`；`DELETE /api/geo/imports/{id}`。来源 `bing_ai`/`bing`/`gsc`，空来源自动识别。
- GEO 官网工具「AI 带来的访问」见 `geo_site.py`：`GET /api/geo/projects/{id}/beacon?origin=` → `{site:{project_id,snippet,installed,last_hit_at,leads_enabled},sources:[{key,name}]}`；`PUT .../beacon {leads_enabled?,reset_code?}` → `{site}`；`GET .../beacon/stats?days=7|30|90` → `{total_visits,ai_visits,ai_leads,series:[{date,ai}],sources:[{key,name,visits,leads}],pages:[{path,visits}]}`；公开 `GET /geo-beacon.js?k=`、`POST /geo-beacon/hit`、`POST /geo-beacon/lead`（CORS `*`、登录门禁前放行）。
- GEO 官网工具「AI 来访记录」见 `geo_site.py`：`POST /api/geo/projects/{id}/crawl-logs`（multipart `file`，Nginx/Apache/JSON 行/gz，30 MB）→ `{ok,duplicate,message,upload}`；`GET .../crawl-logs?page&page_size` → `{items:[{id,filename,range_start,range_end,lines,parsed,hits,created_at}],total}`；`GET .../crawl-stats?days=7|30|90` → `{total,start,end,uploads,note,bots[],purposes[],series[],error_paths[]}`；`DELETE /api/geo/crawl-logs/{id}`。
- GEO 官网体检/生成器见 `geo_site.py`：`POST /projects/{id}/site-audits {url}`（url 省略=项目官网；只能官网域名及子域名；返回 `{ok,reused,audit}`）、`GET /projects/{id}/site-audits?page&page_size`（`items[]` 含 id/url/final_url/score/status/status_name/created_at）、`GET /api/geo/site-audits/{id}`（`audit` 含 checks/bots/summary/page/notice/final_url/finished_at）。生成器 `POST /projects/{id}/generators/robots|llms|schema|entity` → `{ok,result:{kind,file,text,type?,missing,notice?,items?,rows?}}`。体检不执行 JS、私网拒绝、10 秒超时、900KB 上限、同地址 10 分钟复用、默认每天 20 次；`EV_SITE_AUDIT_ALLOW_PRIVATE=1` 仅本地验收。其它官网工具页签（beacon/通知搜索引擎/来访记录/报表导入/商品清单）未接。
- GEO 发布渠道见 `geo_channel.py`：`GET/POST /api/geo/channel-configs`、`PUT/DELETE /api/geo/channel-configs/{id}`、`POST /api/geo/channel-configs/{id}/test`（无 body；返回 `{ok,ok_test,message,channel}`）；`POST /api/geo/contents/{id}/push {channel_id,cover_url}` → `{ok,ok_push,queued,message,publication_id,url?}`（入队；默认最多等 3 秒，快速完成返回 `queued:false` 的最终结果，否则 `queued:true`）；`DELETE /api/geo/publications/{id}` 下线（WordPress 移到回收站；返回 `{ok,remote,message,publication}`）。provider 定义在 `content-meta.channel_kinds`（wordpress/webhook + fields；WordPress 含默认分类/标签字段）；发布记录状态 `queued/running/published/draft_saved/failed/offline`；密钥不回传，编辑留空保持；`geo_channel_configs`/`geo_push_jobs` 按租户；`EV_CHANNEL_ALLOW_PRIVATE=1`、`EV_CHANNEL_SYNC_PUSH=1`（同步推送，测试用）、`EV_CHANNEL_PUSH_WAIT`（默认 3 秒）只用于本地验收/测试。
- 图片：`/api/ext/designer/image/stream`、`/api/ext/ecom-model/image-edit/stream` 等，SSE 事件 `delta/data/usage/error`。
- 登录：`GET /invite` 登录页，`POST /invite` 表单登录（`username/password/code`，`code` 为可选 TOTP），`POST /sign-out` 退出；cookie 名 `ev_commander_session`。登录失败 15 分钟 5 次锁定，管理员可解锁。
- 多租户：前端契约不变，数据按登录会话的租户过滤；本地预览固定演示租户。管理员联调可用请求头 `X-Tenant: <tenant-id>` 切租户，普通账号忽略。
- WhatsApp：前端实际请求 `/scrm-api/api/scrm/...`（scrmService 的 baseURL 是 `/scrm-api`）；webhook 是公开路径 `/webhook/whatsapp`，GET 用验证口令、POST 用 App Secret 校验签名；响应结构见 `whatsapp.py` 的 `read`/`write`。媒体消息的 `media_url` 指向 `/material-media/<key>`；发送先入 `wa_outbox` 队列，前端按 `deliver_status` 轮询 pending/sent/failed。
- 管理后台：页面路径由 `EV_ADMIN_PATH` 配置（留空关闭）；API 为 `/api/admin/me|overview|tenants|users|audit|logins`，以及 `/api/admin/me/security`、`/api/admin/me/totp/setup|enable|disable`；需要 `admin`/`owner` 会话；`mirror/` 不参与。
- 路由入口：`evcommander/serve.py` 的 `send_head`/`do_POST`，以及类后的有序 `READ_ROUTES`/`WRITE_ROUTES` 路由表。
- 算力扣费：响应结构不变；余额不足时对话/生图通过 SSE `error` 事件返回“算力余额不足，请先充值。”；GEO 估算返回 `credits`/`balance`/`enough=false`，前端原有“积分不够”提示生效；消费账单 `type='consume'`、`direction='expense'`、`amount` 为折算金额（算力×0.1）、`power` 为实际算力（可为小数）；对话账单标题带“（X 入 / Y 出）”，按中转站 `usage` 结算，无 usage 用本地估算。

## 8. 给下一个 Agent 的复制提示词

```text
请接手 D:\EVOL-DREAM-线上版-20261119-交接 这个项目。

先按顺序读：
1. NEXT-AGENT-交接.md
2. 运行记录-2026-11-19.md
3. 接手分析-2026-11-19.md
4. 接口契约-7模块-2026-11-19.md

当前分支 dev-多租户-2026-11-20，HEAD 以 git log -1 为准（最近代码提交 1ed7711），基线 tag live-2026-11-19 (3da5376)。

硬性原则：
- 不要改 evcommander/mirror/ 下任何前端文件；
- 后端必须适配现有前端；
- 每完成一个模块更新运行记录并分别提交代码/文档；
- 不提交测试 key、密码、后台安全入口。

当前已完成：文本模型 provider、参考图/图片编辑（含蒙版）、充值订单、获客核心与渠道占位、GEO 项目骨架与模拟体检、**GEO 品牌事实与竞品（`f700568`）**、**GEO 关键词矩阵（`072f61b`）**、**GEO 报告详情与公开分享（`76d7303`）**、**GEO 诊断方案与告警（`356d6c3`）**、**GEO 内容工坊与手工发布（`eb90631`）**、**GEO 品牌知识页与公开站点（`ad906fb`）**、**GEO 引用解析与来源统计（`1f291dd`）**、**GEO 官网体检与 AI 可读性生成器（`299cf91`）**、**GEO 被引用页面拆解（`cce96c2`）**、**GEO 发布渠道 WordPress/Webhook（`89c65d5`：接入设置、密钥隐藏、连接测试、质检内容一键推送、发布记录/草稿）**、**GEO 发布渠道增强（`02f5200`：后台队列、失败重试、封面图、WordPress 分类/标签、下线）**、**GEO 官网工具 AI 来访记录（`26e955b`：访问日志上传解析、AI 爬虫统计、删除）**、**GEO 官网工具 AI 带来的访问（`1673bd5`：统计代码、beacon 上报、来源/页面统计、表单留资）**、**GEO 官网工具搜索平台报表导入（`8862796`：CSV/ZIP、来源识别、明细/删除）**、**GEO 官网工具商品清单（`7398374`：商品 CRUD、CSV 导入、Merchant Center 数据源）**、**GEO 官网工具通知搜索引擎（`1ed7711`：IndexNow、百度收录）**、账单/卡密兑换、路由表抽取与 HTTP 路由回归、多租户第一阶段、WhatsApp Cloud API 服务端骨架、多租户第二阶段、登录安全与业务审计、算力消费扣费（`110df68`）。
工作区应无未提交代码；301 个单元测试通过。
产品核心功能设计草案已出：`EV指挥官-核心功能设计-2026-11-21.md`，其余问题仍待用户拍板；算力扣费已按拍板单价完成（对话 0.05/千入、0.2/千出）。
下一步：官网工具 7 个页签已全部完成；可转公众号/服务号、Shopify/Webflow 等其它发布渠道（需平台凭据/审核）/媒体发稿（需媒体渠道）/拆解升级模型（需预算与扣费口径）/品牌知识页多语言主题（可选）/多租户第三阶段（可选）；真实 IndexNow/百度收录凭据到位后按运行记录第四十五节联调；设计文档剩余问题拍板；真实支付渠道（当前继续 mock）；WhatsApp 联调需要 Meta 商业账号、号码、App Secret、Phone Number ID、Access Token、验证口令和公网 webhook；服务端已就绪，见运行记录第二十二节。
启动命令、测试 API 结论、已知问题都在 NEXT-AGENT-交接.md 和运行记录里。
```
