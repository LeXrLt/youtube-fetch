# Python Pipeline

Python Pipeline 负责发现频道视频、用 `yt-dlp` 获取元数据和字幕、规范化字幕、直接复制
简体中文字幕的翻译字段，并调用 Codex 生成其他语言（包括繁体中文）的简体翻译与结构化分析，最后将全过程写入
PostgreSQL。它只下载字幕，不下载视频
媒体文件。

## 前置条件

- Python 3.12，以及随 Python 提供的 `venv` 和 `pip`。
- 可访问的 PostgreSQL，以及 `psql`、`createdb`、`sha256sum`；运行数据库迁移测试
  还需要 `dropdb`。
- 可从 `PATH` 调用的 `curl` 和 `jq`，用于向 bbs-go 发布并校验 Markdown 内容。
- 已安装且可从 `PATH` 调用的 Codex CLI 0.145 或更高兼容版本，并已完成登录认证。
  可用 `codex --version` 和 `codex login status` 检查。
- Python 依赖固定使用 `codex-sdk-py==0.0.9`。该包是 Codex SDK 的 Python 移植，
  不包含推理后端或 CLI 二进制，而是在运行时调用本机 `codex` 命令。

YouTube 会逐步启用 JavaScript challenge。即使普通链接当前可抓取，部分视频或频道仍
可能要求 `yt-dlp` 支持的 JavaScript 运行时及对应 challenge 组件；遇到 challenge、
签名或播放器解析警告时，应按当前 `yt-dlp` 官方说明补齐运行时，而不要将其误判为
Pipeline 字幕筛选错误。

## 安装

从项目根目录执行：

```bash
python3.12 -m venv pipeline/.venv
pipeline/.venv/bin/python -m pip install -r pipeline/requirements.txt
```

Pipeline 不作为 Python 包安装，也不需要 build、wheel 或 editable install。入口和业务
模块均直接位于 `pipeline/`，所有依赖版本统一由 `pipeline/requirements.txt` 管理。
所有 Python 命令使用 `pipeline/.venv/bin/python`。

根目录 `.env` 提供数据库连接。不要在文档、日志或提交中写入真实凭据：

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=your_user
POSTGRES_PASSWORD=your_password
POSTGRES_DB=youtube_fetch
```

`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` 必填；host 和 port 默认分别为
`localhost`、`5432`。进程环境变量优先于 `.env`。可选的 `CODEX_PATH` 覆盖 Codex
可执行文件路径，`CODEX_MODEL` 覆盖配置中的模型；通常保持为空以使用已认证 CLI 的
默认设置。

BBS 凭据不放在项目根目录 `.env`。Pipeline 读取 portal-push skill 的
`$CODEX_HOME/skills/portal-push/.env`；未设置 `CODEX_HOME` 时默认路径为
`~/.codex/skills/portal-push/.env`。该文件必须是普通文件、权限严格为 `0600`，且只包含
以下两个键各一次；远程地址必须使用 HTTPS，不得提交真实值：

```dotenv
BBS_BASE_URL=https://bbs.example.com
BBS_TOKEN=your_token
```

目标 bbs-go 必须存在唯一的普通分类 `Youtube`，发布账号还须满足站点的发帖、邮箱验证、
观察期和验证码限制。Pipeline 会在写入前检查这些条件。每组三步任务首次领取时会绑定规范化
门户 origin、稳定用户 ID 和分类 ID；后续恢复若检测到站点、账号或分类漂移，会在远程读写前停止。

Cookie 来源默认是 `auto`：macOS 使用当前用户的 Chrome 登录态，其他平台使用 Netscape
Cookie 文件。macOS 不需要导出 Cookie，但 Pipeline 必须由登录 Chrome 的同一用户运行，
登录 Keychain 必须已解锁并允许访问 Chrome Safe Storage。未指定 profile 时，`yt-dlp`
选择最近使用的 Chrome profile；多 profile 环境可在根目录 `.env` 中设置：

```dotenv
YOUTUBE_CHROME_PROFILE=Profile 1
```

非 macOS 平台将导出的 Netscape Cookie 放在
`pipeline/config/youtube.cookies.txt`，并限制为当前用户可读写：

```bash
chmod 600 pipeline/config/youtube.cookies.txt
```

该文件已被 Git 忽略。文件来源下，配置加载时会校验文件存在且权限不向组用户或其他用户
开放。`YOUTUBE_COOKIE_SOURCE=auto|chrome|file` 可覆盖策略，
`YOUTUBE_COOKIE_FILE` 可指定其他私有路径；为兼容旧部署，只设置
`YOUTUBE_COOKIE_FILE` 会自动选择文件来源。Chrome 与文件来源互斥，不会将浏览器 Cookie
写入文件。不得把 Cookie 内容写入日志、测试输出或文档。

Pipeline 会读取 `$CODEX_HOME/config.toml` 中已配置的 MCP server 名称，并在每次 Agent
调用中显式禁用它们。名称只能包含字母、数字、下划线和连字符；无法安全生成禁用覆盖
项时，Pipeline 会拒绝启动 Agent，而不是带着未知工具继续运行。

## 命令

统一入口为：

```bash
pipeline/.venv/bin/python pipeline/main.py [--config PATH] [--log-level LEVEL] COMMAND
```

先校验 `.env`、TOML、提示词和 JSON Schema，不连接数据库或启动 Codex：

```bash
pipeline/.venv/bin/python pipeline/main.py config-check
```

输出当前 profile、Schema 版本和分析 Schema SHA-256。`channel-inspect` 同样不连接或
写入数据库，只通过 `yt-dlp` 验证频道并输出 JSON 元数据：

```bash
pipeline/.venv/bin/python pipeline/main.py channel-inspect '@example'
```

频道输入支持完整 YouTube 频道链接、`@handle`、裸 handle 和以 `UC` 开头的频道 ID。
其他数据库相关命令在执行业务操作前都会自动运行 `db/migrate.sh`；迁移失败时业务操作
不会开始。

抓取和分析使用两把 PostgreSQL 会话级 advisory lock。同一个项目数据库同一时间最多运行
一个下载阶段和一个分析阶段，但下载与分析可彼此并行。`download`、`analyze` 分别持有对应
锁；`run` 和 `video` 依次持有下载锁、释放，再持有分析锁，不会同时占用两把锁。同类锁被
其他进程持有时，新任务立即退出而不排队；连接关闭时 PostgreSQL 自动释放锁。
下载任务被取消时会先等待正在运行的 yt-dlp 工作线程结束，再释放并发配额和下载锁。
`config-check`、`migrate`、`channel-inspect` 和 `channel-add` 不占用阶段锁。

每次 `analyze`、`run` 或 `video` 进入分析锁后、开始分析前，Pipeline 默认清理自身以前
创建且已结束的 Codex Agent 会话。新会话使用项目专属 `youtube_fetch_pipeline` originator；
升级前由本项目创建的 `codex_sdk_py` 会话仍须同时匹配 Pipeline 专用临时目录才兼容清理。
清理器会分页检查活动和归档记录，并同时核对 exec 来源、UUIDv7 会话 ID、临时工作目录和
rollout 首行的结构化 session metadata；rollout 还必须是 Codex 会话目录中的普通文件，任一
条件无法确认都不会删除。任一次列表刷新超过安全上限时都会停止剩余删除。

临时工作目录仍存在的会话会被视为可能仍在运行。每个候选删除前都会重新枚举 Codex 状态；
已加载或存在 fork/子会话的候选会跳过，避免官方 `thread/delete` 的递归删除影响其他会话。
当前协议不支持“仅当未加载且无后代时删除”的原子条件，因此重新枚举与删除之间仍有无法
彻底消除的极短竞态窗口。删除统一通过 Codex app-server 的官方 `thread/delete` 接口完成，
不直接改动 `$CODEX_HOME` 中的文件或索引。

该维护操作由 `[agent].cleanup_historical_sessions` 控制，默认是 `true`；
`session_cleanup_timeout_seconds` 默认是 `300` 且必须大于零。协议、枚举或删除失败会记录不含
会话内容和会话 ID 的计数或警告，并继续本轮分析；任务取消仍会向上传播。纯 `download` 和
`channel-add` 不启动清理。若所用 Codex CLI 不兼容 app-server 会话接口，可暂时关闭该开关，
升级 CLI 后再启用。该功能假设 `CODEX_HOME` 由当前 OS 用户独占；多租户共享同一目录的部署
应关闭自动清理，尤其不能依赖旧 `codex_sdk_py` originator 的兼容识别。

```bash
# 仅创建数据库并应用全部未执行迁移
pipeline/.venv/bin/python pipeline/main.py migrate

# 注册或更新频道；可选地关联研究员
pipeline/.venv/bin/python pipeline/main.py channel-add \
  'https://www.youtube.com/@example' --researcher '研究员姓名'

# 抓取并分析单个视频；内部先持久化字幕，再进入分析阶段
pipeline/.venv/bin/python pipeline/main.py video \
  'https://www.youtube.com/watch?v=VIDEO_ID'

# 只发现视频并下载、规范化字幕，不调用 Codex
pipeline/.venv/bin/python pipeline/main.py download

# 只分析 PostgreSQL 中的待处理字幕，不访问 YouTube；0 表示不限量
pipeline/.venv/bin/python pipeline/main.py analyze --limit 20

# 兼容的一体化命令：全部频道下载完成后，再分析当前全部待处理字幕
pipeline/.venv/bin/python pipeline/main.py run

# 下载阶段只处理显式频道；--channel 可重复，后续分析仍消费全局候选
pipeline/.venv/bin/python pipeline/main.py run \
  --channel 'https://www.youtube.com/@first' \
  --channel 'https://www.youtube.com/@second'

# 下载阶段临时限制每个频道的数量；0 表示不限量
pipeline/.venv/bin/python pipeline/main.py download --max-videos-per-channel 5
```

生产环境建议把两个阶段配置成独立调度任务，例如高频运行 `download`，并按机器可承受的
Codex 负载运行 `analyze --limit N`。两条命令可以同时执行；分析命令启动时会固定本轮候选
视频集合，新加入的视频由下一轮处理。若候选视频在轮到分析前被 `download --force` 写入
新版字幕，本轮会读取执行时的最新版本，完整身份校验仍会防止错误复用。下载已入库后即形成
恢复边界，即使分析失败、被取消或因分析锁竞争而未启动，后续 `analyze` 仍会从 PostgreSQL
继续，不必重新抓取。

命中相同字幕、profile、Schema 版本、提示词版本及相关内容哈希的成功分析时，分析阶段返回
`skipped`。但对于旧数据中翻译字段缺失的有效简体中文字幕，`analyze` 会先原样补齐翻译、源语言
和复制元数据，再允许该匹配分析返回 `skipped`。该修复不调用 Codex。`--force` 作用于命令
包含的阶段：

```bash
pipeline/.venv/bin/python pipeline/main.py video VIDEO_URL --force
pipeline/.venv/bin/python pipeline/main.py run --force
pipeline/.venv/bin/python pipeline/main.py download --force
pipeline/.venv/bin/python pipeline/main.py analyze --limit 20 --force
```

独立 `download --force` 只强制重新抓取，独立 `analyze --force` 只创建新分析 revision；
`run --force` 和 `video --force` 同时作用于两个阶段。对 `analyze --force` 使用不限量设置会
重新分析所有具有有效最新字幕的视频，生产调度应配合 `--limit` 谨慎使用。

## BBS 发布与恢复

`analyze`、`run` 和 `video` 在生成分析时会触发 BBS 发布；`download` 不会。翻译和分析
完成后，`complete_analysis_run` 先在同一数据库事务中提交成功的 analysis revision 以及
`topic`、`translation`、`source` 三步 outbox 快照，事务成功后才向 bbs-go 的 `Youtube`
分类发送 Markdown。主题正文包含视频信息和 AI 分析，第一条一级评论为中文翻译，第二条
一级评论为原文字幕。源语言去除首尾空白、将下划线规范化为连字符并忽略大小写后，只有
`zh`、`zh-Hans`、`zh-CN`、`zh-SG` 被视为简体中文；这些语言跳过原文评论，其他语言包括
繁体中文先由 Agent 翻译为简体中文，并继续发布繁体原文评论。

每次成功写入后都会用远程读取接口回读校验，再推进对应 outbox 状态；已记录远程 ID 的
`created` 步骤可在下次运行时继续回读并完成。仅完成本地领取的 `claimed` 步骤可安全重领，
明确失败且可确认未写入的步骤可恢复执行。
GET 读取允许有限重试，但创建主题和评论的 POST 不自动重试，因为它们不是幂等操作。如果
真正开始 POST 后发生进程中断、请求传输失败或响应不可判定，步骤会标记为 `uncertain`，后续运行停止自动发布；
必须先人工核对远程站点是否已产生主题或评论，再处理数据库状态。该机制用于断点恢复和降低
重复风险，不提供 exactly-once 保证。

门户请求只通过 `curl` 发出，并禁用用户级 `.curlrc`、重定向和跨协议请求；认证 Header 位于
权限受限的临时文件。单个响应上限为 8 MiB，评论回读累计上限为 64 MiB，分页响应在解析后
立即删除。模型生成的分析字段按纯文本写入 Markdown，参考链接只接受 HTTP(S)，避免字幕
提示注入产生可执行 HTML。

功能上线前已经成功完成、因而没有 `bbs_publication_steps` 快照的历史分析不会自动回填。
普通运行会恢复当前匹配 analysis revision 的未完成发布；使用 `--force` 创建的新 revision
拥有自己独立的三步快照并各自发布，可能因此产生新的 BBS 主题。只要同一视频和目标存在
`uncertain` 步骤，`--force` 也不会创建或发布新 revision，必须先完成人工核对。

未指定 `--channel` 时，`download` 和 `run` 从 PostgreSQL 的 `youtube_channels` 表读取
`is_active = true` 的频道；不会读取或同步 Cookie 登录用户的订阅列表。Web 管理页新增或
重新启用的频道会进入默认抓取范围，停用频道及其历史数据仍保留在数据库中。

频道发现固定读取频道 ID 对应的 YouTube Uploads playlist。默认
`max_videos_per_channel = 0`，因此首次回填会枚举完整视频历史；配置正数限制时，每次只登记
指定数量的未发现视频，后续运行继续向旧视频推进。只有实际枚举到 playlist 末尾、且本次
发现的引用全部成功幂等入队后，才立即写入 `initial_backfill_completed_at`。该 marker 与字幕
下载、规范化及分析结果无关，所以字幕随后失败不会撤销首次回填完成状态。

首次回填完成后，普通运行按 Uploads playlist 从最新视频向后扫描，遇到本轮启动前数据库中
已有的 YouTube video ID 就停止；无需再次枚举频道全部历史。这里所有已登记视频都是可信
边界，无论 `subtitle_download_status` 为 `0`、`1` 还是 `2`。状态 `2` 的字幕任务仍由全局
队列在待下载任务之后重试，不依赖频道发现再次越过该视频。增量模式以已知边界为准，即使
设置了正数 `--max-videos-per-channel`，也会先扫描完边界前的全部新增视频。

`--force` 不在已知 video ID 处停止：不限量时重新枚举完整 Uploads playlist，显式设置正数
限制时最多发现该数量的视频，并对本次发现范围内的既有视频强制重新抓取。升级旧数据
时，如果频道已有视频但 `initial_backfill_completed_at` 仍为空，下一次普通运行会再完整扫描
一次（或按显式限制分批扫完）以建立可信增量边界；这是预期的一次性升级成本。

下载阶段按
`[pipeline].download_concurrency` 对频道发现和视频字幕抓取做统一的有界异步并发，默认值
为 `4`，允许范围为 `1` 至 `16`。该限制用于控制并发 HTTP、文件和数据库 I/O；分析阶段
保持逐视频执行，避免同时启动多个 Codex 任务造成额外资源压力。

频道索引发现后先把视频引用写入 PostgreSQL，再生成一次固定的全局下载队列。数值字段
`videos.subtitle_download_status` 使用 `0=待下载`、`1=已下载`、`2=下载失败`：所有 `0`
排在历史 `2` 前面；单视频失败会保存错误、继续后续任务，并只在下一次启动时于队尾重试。
一次运行不会重新读取队列，因此本轮失败不会立即反复执行。即使
`--max-videos-per-channel` 限制了本轮频道发现范围，该频道以前遗留的 `0` 和 `2` 仍会恢复。

每个视频的完整元数据和字幕在下载阶段独立提交。有效规范化简体中文字幕的翻译复制包含在
保存字幕的同一事务中；繁体中文及非中文翻译与分析在后续分析阶段独立提交。
`subtitle_status` 继续区分 `pending`、`fetched`、`unavailable` 和 `invalid` 等内容状态；确认
无字幕或字幕无法规范化也属于下载完成，数值状态为 `1`，普通下载不会重复抓取。分析阶段只
读取每个视频最新且可规范化的字幕；它先修复遗留的简体中文字幕缺失翻译，再跳过已有匹配成功
revision 的视频。失败或取消的分析可在下一轮恢复。

全局 `--log-level` 默认是 `INFO`。默认日志隐藏 yt-dlp 的网页、播放器 API 等底层请求进度，
但保留其警告和错误；使用 `--log-level DEBUG` 才会看到这些底层消息和异常堆栈。`INFO` 直接
记录频道发现模式、发现数量与停止原因，以及字幕队列总数和 `pending`/`retry`/`forced` 分类；
每个字幕任务会记录开始，并明确区分下载成功、未匹配到字幕、字幕无法规范化、跳过和失败。
重试任务在日志上下文中标为 `queue=retry`，无需依赖 yt-dlp 请求明细判断恢复进度。

`run` 保留原参数，但执行顺序固定为“完成所有频道下载，再分析整个数据库的当前候选列表”，
其 JSON 结果是两个阶段结果组成的扁平列表。`--channel` 和
`--max-videos-per-channel` 只约束下载阶段，不限制随后启动的全局分析数量；需要控制 Codex
工作量时应改用独立的 `analyze --limit N`。任一阶段出现 `failed` 时进程退出码为 1；单个
视频或频道失败不会取消同阶段的其他任务。正数 `--max-videos-per-channel` 会分批推进首次
回填，并只在确认 Uploads playlist 已耗尽后标记完成。`analyze --limit 0` 表示不限量，所有
数量限制都拒绝负数。

该队列状态由 `011_subtitle_download_status.sql` 添加；Pipeline 业务命令启动时会自动执行
迁移，独立部署 Web 时需先运行 `./db/migrate.sh`。

`012_backfill_chinese_translations.sql` 为旧库中具有有效规范化文本、语言为 `zh` 或
`zh-*` 且翻译字段缺失的字幕回填原文、源语言和 `copied_chinese_source` 迁移元数据；已有
翻译不会被覆盖。

`013_bbs_publication_steps.sql` 增加不可变的 BBS Markdown outbox 快照、逐步状态、远程 ID、
请求/响应元数据和恢复索引。Pipeline 业务命令会自动应用该迁移；独立部署 Web 或升级调度
前仍须显式运行 `./db/migrate.sh`。

## 字幕选择与处理

默认语言优先级是简体中文、繁体中文、英文。只有 `pipeline.toml` 明确列出的语言和
格式参与选择；每种语言优先人工字幕，再选择自动字幕。默认允许自动字幕，但排除 URL
带 `tlang` 的 YouTube 自动翻译字幕。若不允许自动字幕，将
`allow_automatic_captions` 设为 `false`。

字幕格式默认按 `vtt`、`json3`、`srv3`、`ttml`、`srv2`、`srv1` 排序。未配置格式
不会被选中。抓取到的原始字幕存入 `subtitle_tracks.raw_text`，由数据库自动计算
`raw_sha256`；相同视频、语言、人工/自动属性和原始哈希复用同一行，原文变化则新增
不可变的字幕版本，历史翻译和分析仍指向旧版本。

无法找到合格字幕时状态为 `no_subtitle`。字幕已下载但为空、格式不支持或无法解析时，
原文仍会保存，`normalized_text` 保持空值，状态为 `invalid_subtitle`，不会启动翻译或
分析。规范化成功后，简体中文在 `download` 保存字幕的同一事务中，将
`normalized_text` 原样复制到 `translated_text`，将源语言写入
`translated_language_code`，并记录 `mode = copied_chinese_source` 元数据；此过程不等待
`analyze`，也不调用 Codex。繁体中文和非中文仍在 `analyze` 阶段按配置分块调用 Codex 翻译为简体
中文，再执行分析。

## 提示词与结构化输出

- `config/prompts.toml` 保存翻译和分析模板及各自版本。
- `config/translation.schema.json` 与 `config/analysis.schema.json` 使用 JSON Schema
  Draft 2020-12 约束 Codex 输出；输出在投影和入库前再次校验。
- `config/pipeline.toml` 的 `[projection]` 使用 JSON Pointer 将 Schema 中的字段映射到
  稳定数据库列；标签内的 Pointer 相对于单个标签对象解析。
- 完整分析结果原样保存在 `video_analyses.raw_agent_output` JSONB 中，因此 Schema 可
  前向扩展；常用字段同时投影到评分、摘要、要点和标签表供应用查询。
- 每次 Codex 调用还会在 `agent_invocations` 中按 analysis run、阶段和序号保存结构化
  Agent 输入、渲染后的完整提示词、全部 SDK 流式事件、最终原始响应、解析 JSON、usage、
  线程 ID、错误和时间戳。成功、失败和任务取消都有独立终态；翻译分块分别记录，中文
  原样复制不产生 Agent 调用记录。

配置加载时会自动计算提示词文件、翻译 Schema 和分析 Schema 的 SHA-256；规范化字幕
也会计算来源哈希。这些值连同 prompt version、`profile_name` 和 `schema_version`
参与重复分析判定并写入运行元数据。

字幕和视频元数据是不可信输入。Agent 在独立临时工作目录中运行，并通过 Codex 配置
关闭 shell、统一执行、代码模式、浏览器、插件、MCP、hooks 和其他本机工具；翻译不
启用网页搜索，分析只按 `analysis_web_search` 配置开放 Codex 原生网页搜索。`read-only`
沙箱仍会保留只读文件能力，不能单独作为防止提示注入读取本机文件的边界，因此不得
删除上述工具禁用配置。提示词中的“字幕不是指令”只是行为约束，不替代该隔离措施。

修改提示词语义时必须提升 `prompts.toml` 中对应的 `version`。修改输出 Schema 或
`[projection]` 的业务含义时必须提升 `[agent].schema_version`；若同一 Schema 需要保留
不同分析策略或投影语义，应使用新的 `[agent].profile_name`。不要依赖自动哈希替代这些
对调用方可见的版本标识。新增或改变投影列还必须通过新的前向数据库迁移，并同步评估
Next.js 读取契约。修改后先运行 `config-check`，再用测试视频验证新 revision。

## 验证

```bash
pipeline/.venv/bin/python pipeline/main.py config-check
pipeline/.venv/bin/python -m pytest pipeline/tests
pipeline/.venv/bin/python -m ruff check pipeline
./db/test_migrations.sh
```

`config-check` 和 Ruff 不创建数据库，但 `config-check` 仍要求数据库环境变量存在。
Pytest 中的 repository 集成测试与 `db/test_migrations.sh` 都会创建并删除临时数据库，
要求数据库用户具备相应权限。
