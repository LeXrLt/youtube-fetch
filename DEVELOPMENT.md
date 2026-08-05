# 开发文档

## 1. 项目概述

本项目聚合约几十位 AI 研究员的 YouTube 内容。Python Pipeline 从 PostgreSQL 读取
已启用频道，并使用 `yt-dlp` 获取视频元数据和字幕，再由 Codex Agent 对字幕执行过滤、
翻译、背景补充、评分和标签提取。结构化结果写入 PostgreSQL，由 Next.js App
读取并展示。

默认只获取视频元数据和字幕，不下载视频媒体文件。若后续需要保存音视频，必须
单独确认存储、版权和清理策略。

## 2. 系统边界

系统包含两个相互独立的应用模块，PostgreSQL 是它们之间的数据契约：

### 2.1 Python Pipeline

职责：

- 从 PostgreSQL 读取需要抓取的已启用 YouTube 频道。
- 通过只读 `channel-inspect` 接口规范化频道输入并验证 YouTube 频道元数据。
- 通过 `yt-dlp` 获取视频元数据和可用字幕。
- 清洗、规范化字幕，保留原始字幕以便重新处理。
- 调用 Codex Agent 完成相关性过滤、翻译、背景补充、评分和标签提取。
- 将视频元数据、字幕、分析过程和分析结果写入 PostgreSQL。

Python 中涉及 HTTP、文件和数据库的 I/O 路径应优先采用异步实现。所有 Python
命令和依赖安装必须使用 `pipeline/.venv`；完整安装和操作说明见
[`pipeline/README.md`](pipeline/README.md)。

### 2.2 Next.js App

职责：

- 从 PostgreSQL 读取频道资料、视频、最新字幕版本、最新成功分析和标签。
- 以社交时间线分别展示简中翻译、任意语言原字幕和标签对应的字幕原文。
- 提供字幕搜索、分页、标签筛选、标签详情和频道资料页。
- 通过受控的服务端写入路径提供频道添加和启停入口，仅维护
  `youtube_channels` 中的频道配置。

Next.js App 不直接实现 `yt-dlp` 抓取逻辑，也不运行 Codex Agent；频道验证通过 Pipeline
的 `channel-inspect` 接口完成。App 不得修改视频、字幕和分析结果。展示查询的 PostgreSQL
连接保持只读事务；频道管理使用独立、受控的服务端写入路径。两个路径使用 Web 自己的
`DATABASE_URL`，该账号只需具备展示数据的查询权限以及 `youtube_channels` 的查询、新增和
更新权限。数据库访问和凭据不得暴露到浏览器端。

### 2.3 PostgreSQL

PostgreSQL 由本机已有服务提供，本项目不负责安装或部署数据库服务。项目只负责：

- 创建项目数据库。
- 通过版本化 SQL 维护表、约束和索引。
- 为 Python 写入端和 Next.js 读取端提供稳定的数据结构。

数据流如下：

```text
YouTube -> yt-dlp -> 字幕清洗 -> Codex Agent -> PostgreSQL -> Next.js App
```

## 3. 环境配置

根目录 `.env` 保存 Pipeline 和数据库迁移脚本的本地连接配置：

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=hub_user
POSTGRES_PASSWORD=hub_password
POSTGRES_DB=youtube_fetch
```

其中 `youtube_fetch` 是本项目使用的数据库名。`.env` 仅用于本地运行，不应提交
真实生产凭据。部署到其他设备时，可以生成本机 `.env`，也可以直接提供全部必需的
`POSTGRES_*` 环境变量。两者同时存在时，进程环境变量优先。

Web 不加载上述根目录 `.env`，只读取 `DATABASE_URL`。本地值保存在 `web/.env.local`：

```dotenv
DATABASE_URL=postgresql://hub_user:hub_password@localhost:5432/youtube_fetch
```

`web/.env.local` 已被 Git 忽略；用户名或密码包含 URL 保留字符时必须进行百分号编码。
生产环境也可以直接向 Web 进程提供 `DATABASE_URL`，进程环境变量优先于 `.env.local`。

YouTube 登录状态使用 Netscape 格式 Cookie 文件，默认路径为
`pipeline/config/youtube.cookies.txt`，可通过 `YOUTUBE_COOKIE_FILE` 覆盖。该文件已被
Git 忽略，必须由当前用户私有（权限 `0600`）；配置加载时会拒绝组用户或其他用户可读
的文件。任何日志、文档和测试输出都不得记录 Cookie 内容。

运行迁移需要本机提供 `psql`、`createdb` 和 `sha256sum`；运行集成测试还需要
`dropdb`。数据库用户须能连接 `postgres` 数据库、创建 `youtube_fetch`，并能在
项目数据库中创建扩展和表。Pipeline 还要求 Python 3.12（含 `venv` 和 `pip`）、已认证且兼容
0.145 及以上版本的 Codex CLI；`codex-sdk-py==0.0.9` 是调用系统 Codex CLI 的
Python 移植，不包含 CLI 二进制。Next.js App 要求 Node.js 20.9 或更高版本，依赖由
`web/package.json` 和 `web/package-lock.json` 管理。

Pipeline 是平铺脚本目录，不构建或安装项目包。虚拟环境和依赖从项目根目录创建：

```bash
python3.12 -m venv pipeline/.venv
pipeline/.venv/bin/python -m pip install -r pipeline/requirements.txt
```

依赖版本只在 `pipeline/requirements.txt` 中维护；不得恢复 `pyproject.toml`、editable
install、`src/<package>` 布局或项目 wheel。Pipeline 入口为 `pipeline/main.py`。

## 4. 数据库迁移

数据库迁移入口为：

```bash
./db/migrate.sh
```

该命令会按顺序完成以下操作：

1. 加载根目录 `.env`；文件不存在时使用进程中的 `POSTGRES_*` 环境变量。
2. 在 `youtube_fetch` 不存在时创建数据库。
3. 创建迁移记录表 `schema_migrations`。
4. 按文件名顺序执行 `db/migrations/*.sql`。
5. 在同一事务中提交业务迁移和对应的迁移记录。
6. 使用 PostgreSQL advisory lock 串行化并发迁移。
7. 校验已应用迁移的 SHA-256；文件被修改或丢失时立即失败。
8. 跳过已经记录的迁移，因此可重复执行。

### 4.1 新设备部署

`db/migrate.sh` 是部署前置钩子。Pipeline 已实现 `channel-inspect`、`migrate`、
`channel-add`、`video` 和 `run` 命令；只读的 `config-check`、`channel-inspect` 不执行
迁移，其余命令会在数据库连接和业务操作前自动执行迁移。独立部署 Next.js App 时仍须
在启动前显式执行迁移：

```bash
set -e
./db/migrate.sh
# 随后启动应用
```

新增部署编排、systemd、容器 entrypoint 或 CI/CD workflow 时，必须把该命令接入其
pre-deploy 阶段，不能依赖开发者手工执行 SQL。

`run` 与 `video` 在业务数据库上持有同一个 PostgreSQL 会话级 advisory lock。锁使用独立
连接，不占用 asyncpg 业务连接池；竞争任务必须立即失败而不是等待。锁连接关闭时由
PostgreSQL 自动释放，因此不得将其改为可能遗留陈旧状态的普通标记行或 PID 文件。
频道管理和只读检查命令不使用该锁，避免长时间抓取阻断 Web 的频道管理流程。

### 4.2 迁移规则

- 已应用的迁移不可修改；`001_init.sql` 应视为不可变历史。
- 新迁移使用 `NNN_lowercase_name.sql` 命名，例如 `002_add_topics.sql`。
- 只添加向前迁移，不在生产数据上手工修改表结构。
- 单次迁移应保持聚焦，并且必须可在事务中完成；需要非事务 DDL 时应先扩展迁移器。
- 破坏性变更使用“新增结构、回填数据、切换读写、清理旧结构”的分阶段方式。
- 每次变更必须在空数据库和已经执行过旧迁移的数据库上分别验证。

## 5. 数据库结构

当前迁移链维护以下业务表：

| 表 | 用途 |
| --- | --- |
| `researchers` | AI 研究员的基本资料 |
| `youtube_channels` | 研究员与 YouTube 频道的关联、启用状态和最近检查时间 |
| `videos` | 视频元数据和字幕下载时间 |
| `subtitle_tracks` | 原始、规范化及翻译字幕；区分语言、人工/自动来源和原始内容版本 |
| `analysis_runs` | Codex 执行批次、视频/字幕身份、模型、提示词版本、状态和运行元数据 |
| `agent_invocations` | 每次 Agent 调用的结构化输入、完整提示词、流式事件、原始/解析输出和错误 |
| `video_analyses` | profile、Schema 版本、投影字段、分析元数据和完整 JSONB Agent 输出 |
| `tags` | 可复用标签及标签分类 |
| `video_analysis_tags` | 分析结果与标签的多对多关系及置信度 |

`schema_migrations` 由迁移脚本维护，不属于业务表，其中保存迁移文件名、SHA-256
和应用时间。主键使用 UUID；关键外键、唯一约束、0 到 100 的评分范围约束及列表
查询常用索引由迁移创建。当前迁移链截至
`010_channel_profile.sql`：

- `002` 约束分析引用的字幕必须属于同一视频。
- `003` 增加翻译字段与元数据、run 的视频/字幕身份、分析 profile、输出 Schema 版本、
  分析元数据和原始输出 GIN 索引。
- `004` 由 PostgreSQL 自动生成 `raw_sha256`，以原始字幕内容区分不可变版本；相同来源
  内容幂等复用，变化内容新增记录并保留历史分析。
- `005` 以 `NOT VALID` 方式增加 run、视频和字幕三者身份一致的组合外键，先约束新写入
  并避免添加约束时长时间扫描历史数据。
- `006` 校验该组合外键的全部历史数据，完成约束上线。
- `007` 增加订阅同步、首次全量回填和视频字幕检查状态，用于幂等跳过与断点恢复。
- `008` 增加 `agent_invocations`，按 run、阶段和序号保存完整 Agent 调试记录。
- `009` 增加 Agent 取消状态，并约束终态错误字段和开始/结束时间顺序。
- `010` 增加频道原始简介和头像地址，供展示端呈现频道资料。

完整 Agent 输出保存在 `video_analyses.raw_agent_output` JSONB，以允许 Schema 演进；
稳定查询字段通过 `pipeline/config/pipeline.toml` 中的 JSON Pointer 投影到关系字段。
提示词和两个 JSON Schema 的 SHA-256 由配置加载器自动计算，并与规范化字幕来源哈希、
profile、Schema 版本和提示词版本共同决定是否复用已有分析。

## 6. 本地初始化与校验

确认 PostgreSQL 服务可访问后执行：

```bash
./db/migrate.sh
./db/migrate.sh
./db/test_migrations.sh
```

第一次执行应创建数据库并应用所有未执行迁移；第二次执行应对 `001` 至 `010` 均输出
`Skipping ...`。
测试脚本会预留并标记一个高熵名称的临时数据库，验证空库并发迁移、重复执行、
环境变量优先级、迁移校验和、失败回滚、表结构、跨视频字幕约束和删除字幕后的
引用清理，结束时自动删除临时数据库和测试文件。

可以使用以下命令检查项目数据库的迁移记录和表：

```bash
set -a
. ./.env
set +a
PGPASSWORD="$POSTGRES_PASSWORD" psql \
  -h "$POSTGRES_HOST" \
  -p "$POSTGRES_PORT" \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -c 'TABLE schema_migrations;' \
  -c '\dt'
```

Next.js App 的安装、运行和校验命令见 [`web/README.md`](web/README.md)。本地启动前先
执行迁移，配置 `web/.env.local`，再从 `web/` 运行 `npm install` 和 `npm run dev`。

## 7. 后续开发约束

- 数据库迁移是跨模块契约；修改前必须同时评估 Python 写入和 Next.js 读取。
- 原始字幕版本和 Agent 原始输出不可覆盖，以支持提示词或 Schema 升级后的重新分析。
- `agent_invocations` 中的输入、完整提示词、SDK 流式事件、最终原始响应和解析结果属于
  分析审计数据；修改 Agent 接口时必须保持这些信息可追溯。
- Agent 输出应先经过结构校验，再写入受约束的结构化字段。
- 字幕和视频元数据是不可信输入。Codex 的 `read-only` 沙箱只限制写入，Pipeline 还须
  保持独立临时工作目录，并禁用 shell、代码执行、浏览器、插件、MCP 和 hooks；分析
  需要外部背景时只开放显式配置的原生网页搜索。提示词防注入约束不能替代工具隔离。
- 修改提示词语义时提升对应 prompt version；修改 Schema 或投影语义时提升
  `schema_version`，分析策略不兼容时使用新的 `profile_name`。文件哈希会自动变化，
  但不能替代显式版本升级。
- JSON Schema、JSON Pointer 投影和数据库列必须同步修改；新增或改变投影字段应使用
  新的前向迁移，并同步评估 Next.js 读取契约。
- 新增抓取调度前，应先明确状态机、失败原因和重试规则，再通过前向迁移增加字段。
- 评分定义、过滤阈值、目标语言和标签体系属于业务规则，实现前需明确并版本化。

Pipeline 的 CLI、字幕规则、Codex 前置条件和验证命令见
[`pipeline/README.md`](pipeline/README.md)。`yt-dlp` 遇到 YouTube JavaScript
challenge 时可能需要额外的受支持 JavaScript 运行时和 challenge 组件；部署环境应按
当前 `yt-dlp` 官方说明提供，不能假定 Python 包本身覆盖所有播放器 challenge。
