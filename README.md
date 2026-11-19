# EVOL DREAM 线上版本交接

2026-11-19（新加坡时间）从线上运行目录取样。本包用于娄工程师在本地接手开发。解压后可用 `git log -1` 查看保存的 commit，用 `git show live-2026-11-19` 查看这份版本。请保留本地 Git 快照作为接手基线。

| 目录 | 内容 |
| --- | --- |
| `website/` | 越南公司官网三语页面、前台 Node.js 服务、内容管理后台和现有测试 |
| `evcommander/` | EV 指挥官现有界面构建、静态素材及运行后端 |
| `lead-service/` | 官网生产留资、PostgreSQL、Redis 和邮件任务的现有源码与 Compose 配置 |
| `fixtures/website-admin/` | 官网已发布条目、可公开访问的原始媒体，以及线上图片和视频压缩版本 |
| `deployment/` | 当前部署使用的 Nginx 与 systemd 配置参考 |
| `后端交接清单.md` | 娄工程师负责的后端模块 |

## 本地打开

官网使用 Node.js 18 或更高版本。先在仓库根目录把已发布内容复制到本地数据目录，再启动前台。

```sh
node tools/prepare-website-fixture.cjs
cd website
LOCAL_HEPH_TRIAL=1 node server.js
```

打开 `http://127.0.0.1:4173/`，首页默认越南语。中文的「Agent 平台与部署」可以进入本地指挥官。官网后台在另一个终端启动，首次在本地用交互命令创建管理员。

```sh
cd website
node 管理员账号.cjs 初始化
node backend-server.js
```

后台入口为 `http://127.0.0.1:4181/`。指挥官使用 Python 3.12 或更高版本，在仓库根目录启动。

```sh
cd evcommander
python3 serve.py
```

指挥官入口为 `http://127.0.0.1:8765/mb/home`，本地走回环演示模式。真实对话和文字生图需要开发者自己配置 OpenAI。现有本地代码从 CC Switch 读取 OpenAI 配置，线上邀请模式使用本机密钥文件。具体环境变量可查 `deployment/systemd/evcommander.service`。

生产留资走 `lead-service/` 的 Docker 服务。本地联调前按 `.env.example` 和 `docker-compose.yml` 配置全新的本地数据库与密钥。运行目录里的已发布内容是供开发使用的副本，初始后台账号和会话为空。线上密码、密钥、登录会话、未发布稿件、用户留资、客户资料和指挥官私有数据均保存在各自的生产存储中。

## 交接开发

从 Git 标签 `live-2026-11-19` 创建开发分支，先在本地联调。娄工程师负责的范围见 [后端交接清单](后端交接清单.md)。AI 视频生成暂缓，多平台发布沿用复制文案后手动粘贴。
