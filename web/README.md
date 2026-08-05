# 字幕流 Web

Next.js App 以社交时间线呈现 Pipeline 已写入 PostgreSQL 的频道、视频、字幕和标签，
并通过受控的服务端路径维护频道资料与启用状态。抓取、翻译和分析仍由 `pipeline/`
负责。

## 页面

- `/`：只展示 `translated_language_code = 'zh-CN'` 的非空简中字幕。
- `/subtitles`：展示最新字幕版本的规范化原文，缺失时回退到保存的原始文本。
- `/tags`：展示当前最新成功分析中的标签，可在浏览器内即时筛选。
- `/tags/[tagId]`：展示当前标签对应分析所引用的字幕原文。
- `/channels`：列出全部频道，新增频道，并通过 `is_active` 启用或停用频道。
- `/channels/[channelId]`：原样展示已保存的频道头像、名称、handle 和简介，以及该频道的
  简中字幕。

时间线搜索支持视频标题、频道名称、handle 和字幕正文。列表每页 12 条，并按视频发布
时间倒序排列。历史数据存在多个版本时，普通时间线使用每个视频最新抓取的字幕；标签
使用每个视频最新完成的分析 revision。

## 本地运行

要求 Node.js 20.9 或更高版本，并且根目录 `.env` 已配置 `POSTGRES_*`。频道新增还要求
已安装 `pipeline/.venv` 并配置有效的私有 YouTube Cookie。Web 会通过 `@next/env` 加载
父目录的 `.env`，进程环境变量仍具有更高优先级。

展示查询始终使用只读连接。频道管理可选用独立的最小权限账号；两项必须同时配置，
未配置时兼容使用现有 `POSTGRES_*`：

```dotenv
CHANNEL_ADMIN_POSTGRES_USER=channel_admin
CHANNEL_ADMIN_POSTGRES_PASSWORD=channel_admin_password
```

若 Python 不在默认的 `pipeline/.venv/bin/python`，使用 `PIPELINE_PYTHON` 指定其路径。

```bash
# 在项目根目录执行数据库前向迁移
./db/migrate.sh

cd web
npm install
npm run dev
```

打开 <http://localhost:3000>。生产环境在启动 Web 前同样必须先运行根目录的
`./db/migrate.sh`。

数据库访问集中在 `lib/`，只在服务端执行。展示连接设置
`default_transaction_read_only=on`；独立管理路径只执行频道资料新增、更新和
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
