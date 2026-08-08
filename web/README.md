# 字幕流 Web

Next.js App 以社交时间线呈现 Pipeline 已写入 PostgreSQL 的频道、视频、字幕和标签，
并通过受控的服务端路径维护频道资料与启用状态。抓取、翻译和分析仍由 `pipeline/`
负责。

## 页面

Web 页面必须通过 `/<密钥>` 前缀访问，密钥由 `WEB_ROUTE_KEY` 配置；根路径 `/` 返回
404。以下路径中的 `<密钥>` 均指该配置值：

- `/<密钥>`：展示 `translated_language_code` 为 `zh` 或 `zh-*` 的非空中文字幕。
- `/<密钥>/subtitles`：展示最新字幕版本的规范化原文，缺失时回退到保存的原始文本。
- `/<密钥>/tags`：展示当前最新成功分析中的标签，可在浏览器内即时筛选。
- `/<密钥>/tags/[tagId]`：展示当前标签对应分析所引用的字幕原文。
- `/<密钥>/channels`：列出全部频道，新增频道，并通过 `is_active` 启用或停用频道。
- `/<密钥>/channels/[channelId]`：原样展示已保存的频道头像、名称、handle 和简介，以及该频道的
  中文字幕。
- `/<密钥>/posts/[postId]`：左侧展示对应字幕全文，右侧展示同一字幕版本的最新成功 AI 分析。

时间线搜索支持视频标题、频道名称、handle 和字幕正文。列表每页 12 条，并按视频发布
时间倒序排列。帖子正文折叠连续空白后只预览前 100 个字符，并在底部显示视频标签。历史
数据存在多个版本时，普通时间线使用每个视频最新抓取的字幕；标签使用每个视频最新完成的
分析 revision。频道摘要、侧栏和管理页的字幕数量均统计该频道当前可展示的中文字幕，与
频道详情列表使用相同口径。

## 本地运行

要求 Node.js 20.9 或更高版本。Web 的数据库连接只读取 `DATABASE_URL`，本地配置保存在
`web/.env.local`，不读取项目根目录 `.env` 中的 `POSTGRES_*`。先生成 32 位路由密钥：

```bash
openssl rand -hex 16
```

将命令输出和数据库连接写入 `web/.env.local`，不要提交真实密钥或凭据：

```dotenv
DATABASE_URL=postgresql://hub_user:hub_password@localhost:5432/youtube_fetch
WEB_ROUTE_KEY=<生成的 32 位密钥>
```

`.env.local` 已被 Git 忽略；用户名或密码包含 URL 保留字符时必须进行
百分号编码。展示查询和频道管理使用同一连接 URL，数据库账号需要具备展示数据的查询权限，
以及 `youtube_channels` 的查询、新增和更新权限。展示连接仍会设置为只读会话。

频道新增还要求已安装 `pipeline/.venv` 并配置有效的私有 YouTube Cookie。

若 Python 不在默认的 `pipeline/.venv/bin/python`，使用 `PIPELINE_PYTHON` 指定其路径。

```bash
# 在项目根目录执行数据库前向迁移
./db/migrate.sh

cd web
npm install
npm run dev
```

打开 `http://localhost:3000/<密钥>`；访问 `http://localhost:3000/` 将返回 404。生产环境
在启动 Web 前同样必须先运行根目录的 `./db/migrate.sh`。`npm run build` 和运行构建产物时
必须使用相同的 `WEB_ROUTE_KEY`。

数据库访问集中在 `lib/`，只在服务端执行。展示连接设置
`default_transaction_read_only=on`；管理路径只执行频道资料新增、更新和
`is_active`。浏览器不会收到数据库凭据或原始数据库行。频道不存在或输入格式无效时，
新增表单会在输入框下显示“频道不存在！”。

## 校验

```bash
npm run lint
npm run typecheck
npm test
npm run build

# 首次运行端到端测试前安装浏览器
npx playwright install chromium
npm run test:e2e
```

端到端测试读取当前项目数据库，需要库中已有频道、字幕、标签以及带头像和简介的频道
资料。测试默认自行在 `3212` 端口启动开发服务器；若已有服务器在其他端口运行，使用
对应端口，例如 `WEB_E2E_PORT=3000 npm run test:e2e`。
