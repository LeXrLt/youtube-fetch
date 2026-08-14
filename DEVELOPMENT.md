# 开发文档

## 1. 项目概述

本项目聚合约几十位 AI 研究员的 YouTube 内容。Python Pipeline 从 PostgreSQL 读取
已启用频道，并使用 `yt-dlp` 获取视频元数据和字幕，再由 Codex Agent 对非简体中文字幕执行
翻译，并对字幕执行过滤、背景补充、评分和标签提取。结构化结果写入 PostgreSQL，由
Next.js App 读取并展示。

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
- 对有效规范化简体中文字幕直接复制翻译字段；调用 Codex Agent 翻译繁体及非中文字幕，并完成
  相关性过滤、背景补充、评分和标签提取。
- 将视频元数据、字幕、分析过程和分析结果写入 PostgreSQL。
- 在成功提交分析和不可变 outbox 快照后，通过 bbs-go 将 AI 分析、翻译和按语言规则保留的
  原文发布到 `Youtube` 分类，并回读校验远程结果。

Python 中涉及 HTTP、文件和数据库的 I/O 路径应优先采用异步实现。所有 Python
命令和依赖安装必须使用 `pipeline/.venv`；完整安装和操作说明见
[`pipeline/README.md`](pipeline/README.md)。

### 2.2 Next.js App

职责：

- 从 PostgreSQL 读取频道资料、视频、最新字幕版本、最新成功分析和标签。
- 以社交时间线分别展示中文字幕、任意语言原字幕和标签对应的字幕原文。
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

下载与分析以 PostgreSQL 中已提交的视频和字幕为持久边界，数据流如下：

```text
YouTube -> yt-dlp -> 字幕清洗 -> 简体中文字幕翻译字段复制 -> PostgreSQL
PostgreSQL -> Codex Agent -> PostgreSQL -> Next.js App
                                  -> bbs-go Youtube 分类
```

下载阶段完成后不等待 Agent 即可释放下载调度资源；分析阶段只读取数据库，不访问 YouTube。
因此两个阶段可由独立进程并行运行，分析积压不会阻止后续频道继续发现和下载字幕。

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

Web 不加载上述根目录 `.env`，读取 `DATABASE_URL` 和 `WEB_ROUTE_KEY`。本地值保存在
`web/.env.local`，路由密钥使用 `openssl rand -hex 16` 生成：

```dotenv
DATABASE_URL=postgresql://hub_user:hub_password@localhost:5432/youtube_fetch
WEB_ROUTE_KEY=<生成的 32 位密钥>
```

`web/.env.local` 已被 Git 忽略；用户名或密码包含 URL 保留字符时必须进行百分号编码。Web
页面通过 `/<密钥>` 访问，根路径返回 404。生产环境也可以直接向 Web 进程提供这两个变量，
进程环境变量优先于 `.env.local`；构建和运行必须使用相同的 `WEB_ROUTE_KEY`。

BBS 使用 portal-push skill 的独立凭据文件：`$CODEX_HOME/skills/portal-push/.env`；没有
`CODEX_HOME` 时读取 `~/.codex/skills/portal-push/.env`。它必须是非符号链接的普通文件、
权限严格为 `0600`，且只定义 `BBS_BASE_URL` 和 `BBS_TOKEN` 各一次。远程
`BBS_BASE_URL` 必须是 HTTPS origin。不要把真实 BBS 地址或 token 写入项目 `.env`、文档、
日志或测试输出。

YouTube Cookie 来源由 `[youtube].cookie_source` 控制，默认 `auto`：macOS 直接读取当前
用户的 Chrome Cookie，其他平台读取 `pipeline/config/youtube.cookies.txt`。macOS 使用
Chrome 时，运行 Pipeline 的系统用户必须与登录 Chrome 的用户相同，并允许进程访问已解锁
的登录 Keychain；未指定 profile 时由 `yt-dlp` 选择最近使用的 Chrome profile。

`YOUTUBE_COOKIE_SOURCE=chrome|file|auto` 可覆盖来源，`YOUTUBE_CHROME_PROFILE` 可固定
`Default`、`Profile 1` 等 profile。为了兼容原配置，仅显式设置
`YOUTUBE_COOKIE_FILE` 时会自动选择 `file`；Cookie 文件必须为 Netscape 格式、权限
`0600`。Chrome 和文件来源互斥，不会合并或把浏览器 Cookie 导出到文件。任何日志、文档
和测试输出都不得记录 Cookie 内容。

运行迁移需要本机提供 `psql`、`createdb` 和 `sha256sum`；运行集成测试还需要
`dropdb`。数据库用户须能连接 `postgres` 数据库、创建 `youtube_fetch`，并能在
项目数据库中创建扩展和表。Pipeline 还要求 Python 3.12（含 `venv` 和 `pip`）、已认证且兼容
0.145 及以上版本的 Codex CLI；`codex-sdk-py==0.0.9` 是调用系统 Codex CLI 的
Python 移植，不包含 CLI 二进制。BBS 客户端要求 `curl` 和 `jq` 可从 `PATH` 调用。
Next.js App 要求 Node.js 20.9 或更高版本，依赖由
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
`channel-add`、`download`、`analyze`、`video` 和 `run` 命令；只读的 `config-check`、
`channel-inspect` 不执行迁移，其余命令会在数据库连接和业务操作前自动执行迁移。独立部署
Next.js App 时仍须在启动前显式执行迁移：

```bash
set -e
./db/migrate.sh
# 随后启动应用
```

新增部署编排、systemd、容器 entrypoint 或 CI/CD workflow 时，必须把该命令接入其
pre-deploy 阶段，不能依赖开发者手工执行 SQL。

下载和分析在业务数据库上使用名称不同的 PostgreSQL 会话级 advisory lock。`download`
持下载锁，`analyze` 持分析锁；`run` 和 `video` 按下载、分析顺序分别持锁，中间先释放下载
锁。同类任务互斥并在竞争时立即失败，下载与分析任务可同时运行。锁使用独立连接，不占用
asyncpg 业务连接池；连接关闭时由 PostgreSQL 自动释放，因此不得将其改为可能遗留陈旧
状态的普通标记行或 PID 文件。频道管理和只读检查命令不使用阶段锁。

`[pipeline].download_concurrency` 对频道发现与视频字幕抓取共享一个异步并发上限，默认
为 `4`，配置校验范围是 `1..16`。分析保持单视频顺序执行。生产调度应优先分别运行
`download` 和 `analyze --limit N`；`run` 用于兼容原有一次性流程，但也会先完成所有下载，
再分析数据库中的全局候选视频集合；`run` 的频道和每频道数量参数只约束下载阶段。

频道发现通过频道 ID 对应的 Uploads playlist 完成。首次回填不限量时完整枚举；设置正数
`max_videos_per_channel` 时按未发现视频数量分批推进。只有枚举到 playlist 末尾且本次引用
全部成功幂等入队后，才立即写 `initial_backfill_completed_at`，不等待字幕下载或分析。首次
回填完成后的普通运行从最新视频向后扫描，遇到运行前已登记的 video ID 即停止；`0`、`1`、
`2` 三种字幕下载状态都构成已知边界。`--force` 不在已知 ID 处停止，按不限量或显式限制
重新扫描。旧数据若 marker 为空，会再执行一次完整扫描（也可分批完成）建立可信边界。

Pipeline 先把发现的视频引用幂等登记到 `videos`，再一次性读取所选频道的全局下载候选快照。
`subtitle_download_status` 是独立的数值调度状态：`0` 表示待下载，`1` 表示
下载检查完成，`2` 表示下载失败。队列先调度全部 `0`，再按失败检查时间调度历史 `2`；本轮
新失败只写入状态和错误信息，不重新查询或重新入队，因此会继续处理后续任务并在下次启动时
排到待下载任务之后重试。`unavailable` 和 `invalid` 仍保留在原有 `subtitle_status` 中，且
对应数值状态 `1`，避免把“确认无字幕”或“已下载但无法规范化”误当成传输失败反复抓取。
有效规范化简体中文字幕在保存字幕的同一事务中将 `normalized_text` 原样复制到
`translated_text`，同时写入源语言和 `copied_chinese_source` 元数据，不等待 `analyze`，
也不调用 Codex。繁体中文及非中文字幕仍由后续 `analyze` 阶段调用 Codex 翻译。字幕下载
完成即提交到 PostgreSQL，后续分析失败或取消时可独立恢复；`analyze` 遇到旧数据中缺失翻译的有效简体中文
字幕时，会先补齐同样的复制结果，再允许命中既有成功分析并返回 `skipped`。

分析发布采用三步持久化 outbox。`complete_analysis_run` 在提交成功 analysis revision 的
同一事务中写入 `topic`、`translation`、`source` 的不可变 Markdown 快照，提交后才依次向
bbs-go 写入“AI 分析主题、中文翻译一级评论、原文字幕一级评论”并逐步回读验证。源语言经
trim、下划线转连字符和大小写规范化后，仅 `zh`、`zh-Hans`、`zh-CN`、`zh-SG` 跳过原文
评论；繁体中文会翻译为简体并保留繁体原文，其他语言均保留原文评论。`analyze`、`run`、
`video` 会触发发布，`download` 不会。

远程 GET 可有限重试，非幂等 POST 不自动重试。只完成本地领取的 `claimed` 可安全重领，
`created` 状态可依照已保存远程 ID 恢复回读，明确未写入的 `failed` 状态可再次执行；真正开始
POST 后中断或传输结果不明会进入 `uncertain`，必须人工检查
远程主题或评论，不能自动重放。这不是 exactly-once 协议。上线前没有 outbox 行的历史成功
分析不会回填；每个 `--force` 新 revision 都生成并发布自己的一组三步快照，但同一视频和目标
存在 `uncertain` 时必须先人工核对，不能用 `--force` 绕过。

首次领取会把规范化门户 origin、稳定用户 ID、分类 ID 和账号名写入不可变目标元数据；恢复
时先在任何认证请求前校验 origin，再核对账号和分类，配置不一致时禁止继续。`curl` 禁用默认
配置和重定向；单响应限制为 8 MiB，评论回读累计限制为 64 MiB且逐页清理。模型派生字段按
纯文本转义，外部参考链接仅允许 HTTP(S)。

运行日志以业务状态为可观测边界。默认 `INFO` 记录频道发现的模式、数量和停止原因，以及
字幕任务的开始、成功、无匹配字幕、无法规范化、跳过、失败和 `retry` 队列来源。yt-dlp 的
网页和播放器请求进度统一降为 `DEBUG`，警告与错误仍保留原级别；异常堆栈也仅在 `DEBUG`
展开，避免默认日志被底层请求淹没。

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
| `videos` | 视频元数据、字幕内容状态、数值下载调度状态和最近下载错误 |
| `subtitle_tracks` | 原始、规范化及翻译字幕；区分语言、人工/自动来源和原始内容版本 |
| `analysis_runs` | Codex 执行批次、视频/字幕身份、模型、提示词版本、状态和运行元数据 |
| `agent_invocations` | 每次 Agent 调用的结构化输入、完整提示词、流式事件、原始/解析输出和错误 |
| `video_analyses` | profile、Schema 版本、投影字段、分析元数据和完整 JSONB Agent 输出 |
| `tags` | 可复用标签及标签分类 |
| `video_analysis_tags` | 分析结果与标签的多对多关系及置信度 |
| `bbs_publication_steps` | 每个 analysis revision 的三步不可变 BBS Markdown 快照、发布状态、远程 ID 和验证元数据 |

`schema_migrations` 由迁移脚本维护，不属于业务表，其中保存迁移文件名、SHA-256
和应用时间。主键使用 UUID；关键外键、唯一约束、0 到 100 的评分范围约束及列表
查询常用索引由迁移创建。当前迁移链截至
`013_bbs_publication_steps.sql`：

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
- `011` 增加数值字幕下载状态和最近错误，回填历史数据，并为待下载优先、失败队尾重试建立
  部分索引。
- `012` 为旧库中具有有效规范化文本但缺失翻译的中文（`zh` 或 `zh-*`）字幕回填原文、
  源语言和 `copied_chinese_source` 迁移元数据；已有翻译不覆盖。
- `013` 增加 `bbs_publication_steps`，约束每个 analysis revision 的主题、翻译和原文三步
  快照及状态，禁止修改目标与内容快照，并为待恢复步骤建立部分索引。

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

第一次执行应创建数据库并应用所有未执行迁移；第二次执行应对 `001` 至 `013` 均输出
`Skipping ...`。
测试脚本会预留并标记一个高熵名称的临时数据库，验证空库并发迁移、重复执行、
环境变量优先级、迁移校验和、失败回滚、表结构、下载状态约束与历史回填、中文翻译回填、
BBS 发布步骤约束与快照不可变性、跨视频字幕约束和删除字幕后的引用清理，结束时自动删除
临时数据库和测试文件。

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
