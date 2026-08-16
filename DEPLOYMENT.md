# 部署说明

本项目由 PostgreSQL、Python Pipeline 和 Next.js Web 三部分组成：Pipeline 负责抓取和写入内容，Web 负责展示并维护频道配置。

## 环境要求

- PostgreSQL、`psql`、`sha256sum`
- Python 3.12
- Node.js 20.9 或更高版本
- 运行 Pipeline 时需要已登录的 Codex CLI 和有效的 YouTube Cookie
- 运行 BBS 发布时需要可从 `PATH` 调用的 `curl` 和 `jq`

## 配置

在项目根目录创建供 Pipeline 和迁移脚本使用的 `.env`：

```dotenv
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password
POSTGRES_DB=youtube_fetch
```

Web 不读取根目录 `.env`。生成一个 32 位路由密钥：

```bash
openssl rand -hex 16
```

在 `web/.env.local` 中配置独立的连接 URL 和命令生成的密钥：

```dotenv
DATABASE_URL=postgresql://hub_user:hub_password@localhost:5432/youtube_fetch
WEB_ROUTE_KEY=<生成的 32 位密钥>
```

该 URL 中的账号需要具备展示数据的查询权限，以及 `youtube_channels` 的查询、新增和更新
权限。生产环境也可以直接通过环境变量向 Web 构建和运行进程提供 `DATABASE_URL` 与
`WEB_ROUTE_KEY`；不得提交真实凭据或密钥。构建和运行必须使用同一个 `WEB_ROUTE_KEY`。

`pipeline/config/pipeline.toml` 默认设置 `cookie_source = "auto"`。在 macOS 上，Pipeline
优先使用当前用户已登录 YouTube 的 Chrome Cookie，不要求导出文件。Pipeline 必须由该
Chrome 用户运行，登录 Keychain 需要保持解锁，并在系统询问时授权访问 Chrome Safe
Storage。多个 Chrome profile 时可在根目录 `.env` 中指定：

```dotenv
YOUTUBE_CHROME_PROFILE=Profile 1
```

非 macOS 平台的 `auto` 使用 Netscape Cookie 文件。也可以在任意平台显式切换到文件来源：

```bash
export YOUTUBE_COOKIE_SOURCE=file
chmod 600 pipeline/config/youtube.cookies.txt
```

`YOUTUBE_COOKIE_SOURCE=chrome` 可强制读取 Chrome；只设置旧的 `YOUTUBE_COOKIE_FILE`
仍会自动选择文件来源。两种来源不会同时传给 `yt-dlp`。`config-check` 只校验配置，不访问
Chrome 或 Keychain；macOS 部署后应运行一次 `channel-inspect` 或小批量 `download` 验证
系统授权和目标 profile。

在 portal-push skill 目录创建独立的 BBS 凭据文件。设置 `CODEX_HOME` 时路径为
`$CODEX_HOME/skills/portal-push/.env`，否则为 `~/.codex/skills/portal-push/.env`：

```dotenv
BBS_BASE_URL=https://bbs.example.com
BBS_TOKEN=your_token
```

文件必须是普通文件而非符号链接，严格限制为 `chmod 600`，并且只能包含上述两个键各一次。
生产 BBS 地址必须使用 HTTPS。不要复用项目根目录 `.env`，也不要把真实地址或 token 写入
仓库、日志或部署输出。站点中必须存在唯一的普通分类 `Youtube`；部署前还应确认账号可发
主题和评论，且满足站点邮箱验证、观察期和验证码策略。

`pipeline/config/pipeline.toml` 中的下载并发默认为：

```toml
[pipeline]
download_concurrency = 4
```

允许范围为 `1..16`。该上限同时约束频道发现和字幕下载的异步 I/O；应结合网络、数据库连接
池和主机容量调整。分析保持逐视频运行，不受该配置影响。

## 安装与构建

在项目根目录执行：

```bash
python3.12 -m venv pipeline/.venv
pipeline/.venv/bin/python -m pip install -r pipeline/requirements.txt

./db/migrate.sh

cd web
npm ci
npm run build
cd ..
```

部署新版本时，应在启动应用前再次执行 `./db/migrate.sh`。

## 启动

兼容原有方式运行一次完整 Pipeline；该命令先完成所有下载，再开始分析：

```bash
pipeline/.venv/bin/python pipeline/main.py run
```

生产环境更适合将三个阶段作为独立调度任务：

```bash
# 高频发现视频并下载字幕，不调用 Codex
pipeline/.venv/bin/python pipeline/main.py download

# 消费数据库中的待分析字幕；0 表示不限量
pipeline/.venv/bin/python pipeline/main.py analyze --limit 20

# 消费成功分析并向 BBS 发布；0 表示不限量
pipeline/.venv/bin/python pipeline/main.py push --limit 20
```

有效规范化的简体中文字幕会在 `download` 保存字幕的同一数据库事务中原样写入
`translated_text`、源语言和复制元数据，不等待 `analyze`，也不调用 Codex。繁体中文和
非中文字幕仍由 `analyze` 调用 Codex 翻译为简体中文。下载结果在进入分析前已经提交到
PostgreSQL。分析
任务失败、取消或暂时未取得锁时，后续 `analyze` 可直接从数据库继续，不需要重新访问
YouTube。下载、分析和发布三个命令可以同时运行；分析启动
时会固定候选视频集合，发布启动时也通过单次查询固定 analysis revision 快照；新加入的视频或
revision 留给下一轮。候选视频在执行前若被强制刷新，则读取其
最新字幕。`analyze` 会先修复旧数据中缺失翻译的有效简体中文字幕，再允许匹配的既有成功分析
返回 `skipped`。可分别设置不同的触发频率和超时，避免大量 Codex 任务拖延新字幕抓取。

`analyze`、`run` 和 `video` 只落库，不访问 bbs-go。分析成功时保存当次中文翻译字幕的不可变
快照；只有 `push` 从成功分析队列中消费并向 bbs-go 发布。目标为 `Youtube` 分类：主题正文
以摘要开头，然后包含视频信息和 AI 分析；第一条一级评论是中文翻译，第二条
一级评论是原文。源语言经空白、下划线和大小写规范化后，只有 `zh`、`zh-Hans`、`zh-CN`、
`zh-SG` 跳过原文评论；繁体中文会翻译为简体，并与其他语言一样继续发布原文。
主题标题为“原语言嘉宾姓名（简体中文身份）｜频道｜YYYY-MM-DD”；多嘉宾按主嘉宾优先、
其次首次出现顺序排列，无嘉宾时回退原视频标题，缺身份、频道或日期时使用明确的未知占位。

启动 Web 服务：

```bash
cd web
npm run start -- --hostname 0.0.0.0 --port 3000
```

Web 必须通过 `/<密钥>` 访问，根路径 `/` 返回 404。反向代理必须原样转发完整路径，不得
剥离密钥前缀；健康检查也应请求带密钥的路径。该路由密钥适用于内部访问约束，不替代鉴权，
不要在文档、日志或公开配置中记录实际密钥。

生产环境中应使用外部调度器周期运行 Pipeline，并使用进程管理器保持 Web 服务常驻。
PostgreSQL advisory lock 保证同一数据库最多同时运行一个下载、一个分析和一个发布阶段；
同类任务重叠时后触发者立即失败且不会排队，三个阶段则可并行。`download`、`analyze`、`push`
分别使用下载、分析、publication advisory lock。`run` 和 `video` 也按下载、分析阶段依次
获取对应锁，不会获取 publication 锁。`config-check`、`migrate`、`channel-inspect` 和
`channel-add` 不占用阶段锁。

旧版本只使用单一的 `pipeline_process` 锁，与新版阶段锁互不识别。升级时必须先停止旧版
`run`/`video` 调度并等待进程完全退出，再启用新版 `download`/`analyze`/`push` 调度；不得让两个
版本滚动混跑。`run` 的频道和每频道数量参数只限制下载阶段，随后仍会分析数据库中的全局
候选；生产环境需要限制 Codex 工作量时使用独立的 `analyze --limit N`。

升级版本包含 `011_subtitle_download_status.sql`：它增加 `0=待下载`、`1=已下载`、
`2=下载失败` 的持久化调度状态和错误字段。部署入口必须先运行 `./db/migrate.sh`；Pipeline
业务命令也会在连接数据库前自动执行迁移。下载失败只影响当前视频，本轮继续处理队列；下一
次启动时先执行待下载任务，再重试历史失败任务。

升级版本还包含 `012_backfill_chinese_translations.sql`：它为旧库中具有有效
`normalized_text`、语言为 `zh` 或 `zh-*` 且翻译字段缺失的字幕回填原文、源语言和
`copied_chinese_source` 迁移元数据，不覆盖已有翻译。该历史迁移覆盖全部 `zh-*`；新版分析会
把其中的繁体复制结果视为待重新翻译。应在启用新版 Pipeline 或 Web 前完成迁移。

升级版本还包含 `013_bbs_publication_steps.sql`，用于保存主题、翻译和原文三步不可变
Markdown outbox 快照、状态、远程 ID 与验证元数据。新流程不在分析事务内创建 outbox，而由
`push` 首次消费时构建。

`014_analysis_translation_snapshot.sql` 为成功分析增加不可变翻译字幕快照，并尝试回填
元数据一致、可靠重建的历史 revision。部署时必须先运行 `./db/migrate.sh`，再启用
新 Pipeline 与 Web。旧 revision 缺少可靠快照或结构化 `guests` 时不会首次入队；需要用
当前 analysis prompt v3 和 `schema_version = "2"` 重新分析。升级前已存在的未完成 outbox
仍由 `push` 恢复，不受首次入队条件限制。

发布按“消费成功分析、首次初始化三步快照、创建后回读验证、从持久状态恢复”的边界运行。
读取请求可有限
重试，创建主题和评论的 POST 不自动重试。只完成本地领取的 `claimed` 步骤可安全重领；已保存
远程 ID 的步骤可继续回读，明确失败且确认
未写入的步骤可在后续 `push` 恢复；进程中断或远程结果不确定时步骤会
进入 `uncertain`，调度会停止自动重放该视频。已有 `uncertain` 或遗留 `in_progress` 会作为
失败诊断输出并使命令返回非零，但不占正数 `--limit` 的新发布额度；同视频后续 revision 保持
阻塞，其他视频继续处理，所以 JSON 结果数可能大于 limit。此时必须人工在 BBS 核对是否已有对应主题或评论，
再决定如何处理状态，否则可能重复发布。该流程不保证 exactly-once。

首次领取还会绑定规范化门户 origin、稳定用户 ID 和 `Youtube` 分类 ID。轮换同一账号的 token
不影响恢复，但切换站点、账号或分类会在远程读写前停止。Portal 客户端禁用 `.curlrc` 和重定向，
单个响应限制为 8 MiB，评论回读累计限制为 64 MiB且逐页清理；模型生成的分析文本按纯文本
发布，参考链接仅接受 HTTP(S)。
不得改变 portal-push 凭据路径和 `0600` 权限约束；门户请求和 JSON 处理继续只使用 `curl`
与 `jq`，不要把真实凭据写入项目 `.env`、调度命令或日志。

频道发现使用 Uploads playlist。新频道会完整回填，或按正数 `max_videos_per_channel` 分批
回填；完整枚举并成功入队后立即记录 `initial_backfill_completed_at`，不等待字幕成功。此后
普通调度从最新视频扫描到运行前已知 video ID 即停止，`0`、`1`、`2` 都是边界，状态 `2`
仍由下载队列在队尾重试。`--force` 保持完整扫描或服从显式数量限制。旧库中已有视频但首次
回填 marker 为空的频道，升级后会额外完整扫描一次（设置限制时分批扫完）来建立可信边界；
评估首次部署窗口时应计入这次一次性 YouTube 请求量。

生产日志建议保持默认 `--log-level INFO`：它记录频道发现、字幕任务开始、成功、无字幕、
无效字幕、失败及重试队列等业务状态，同时隐藏 yt-dlp 的底层网页和播放器请求。排障时临时
切换为 `--log-level DEBUG` 查看底层请求和异常堆栈；yt-dlp 警告与错误在默认级别仍可见。
日志不得包含 Cookie 或数据库凭据。若需要在 Web 中新增频道，必须保留
`pipeline/.venv` 和私有 YouTube Cookie；仅作只读展示时可跳过 Pipeline 的安装和运行。
