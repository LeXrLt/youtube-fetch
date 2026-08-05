# Python Pipeline

Python Pipeline 负责发现频道视频、用 `yt-dlp` 获取元数据和字幕、规范化字幕、调用
Codex 生成结构化翻译与分析，并将全过程写入 PostgreSQL。它只下载字幕，不下载视频
媒体文件。

## 前置条件

- Python 3.12，以及随 Python 提供的 `venv` 和 `pip`。
- 可访问的 PostgreSQL，以及 `psql`、`createdb`、`sha256sum`；运行数据库迁移测试
  还需要 `dropdb`。
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

将登录 YouTube 后导出的 Netscape Cookie 文件放在
`pipeline/config/youtube.cookies.txt`，并限制为当前用户可读写：

```bash
chmod 600 pipeline/config/youtube.cookies.txt
```

该文件已被 Git 忽略，配置加载时会校验文件存在且权限不向组用户或其他用户开放。
部署环境可用 `YOUTUBE_COOKIE_FILE` 指向其他私有路径。不得把 Cookie 内容写入日志、
测试输出或文档。

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

```bash
# 仅创建数据库并应用全部未执行迁移
pipeline/.venv/bin/python pipeline/main.py migrate

# 注册或更新频道；可选地关联研究员
pipeline/.venv/bin/python pipeline/main.py channel-add \
  'https://www.youtube.com/@example' --researcher '研究员姓名'

# 抓取并分析单个视频
pipeline/.venv/bin/python pipeline/main.py video \
  'https://www.youtube.com/watch?v=VIDEO_ID'

# 未指定频道时，处理 youtube_channels 表中全部启用频道
pipeline/.venv/bin/python pipeline/main.py run

# 只处理显式频道；--channel 可重复
pipeline/.venv/bin/python pipeline/main.py run \
  --channel 'https://www.youtube.com/@first' \
  --channel 'https://www.youtube.com/@second'

# 临时限制每个频道的数量；0 表示不限量
pipeline/.venv/bin/python pipeline/main.py run --max-videos-per-channel 5
```

命中相同字幕、profile、Schema 版本、提示词版本及相关内容哈希的成功分析时，`video`
和 `run` 返回 `skipped`。`--force` 可显式创建新的分析 revision：

```bash
pipeline/.venv/bin/python pipeline/main.py video VIDEO_URL --force
pipeline/.venv/bin/python pipeline/main.py run --force
```

未指定 `--channel` 时，`run` 从 PostgreSQL 的 `youtube_channels` 表读取
`is_active = true` 的频道；不会读取或同步 Cookie 登录用户的订阅列表。Web 管理页新增或
重新启用的频道会进入默认抓取范围，停用频道及其历史数据仍保留在数据库中。

默认 `max_videos_per_channel = 0`，因此首次发现频道时枚举完整视频历史；只有不限量扫描
且本频道没有失败时，才会记录首次回填完成时间。每个视频的元数据、字幕、翻译和分析
均独立提交。后续运行先按 YouTube video ID 查询 PostgreSQL：完整分析直接跳过，失败的
分析从已保存字幕或匹配版本的翻译继续，无字幕和无效字幕状态也不重复抓取。进程中断后
重新执行同一命令即可恢复；`--force` 会绕过这些复用状态并重新抓取、翻译和分析。
为发现新增视频，普通运行仍会枚举频道索引，但不会再次下载或分析已命中的视频。

`run` 逐频道、逐视频处理；单个视频或频道失败会记录为 `failed` 并继续，结果中只要存在
失败，进程退出码即为 1。正数 `--max-videos-per-channel` 只用于开发或运维抽样，不会把
频道标记为已完成首次全量回填。

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
分析。规范化成功后，中文（包括简体和繁体）原样复制到翻译字段，不调用翻译 Agent；
非中文按配置分块调用 Codex 翻译为简体中文，再执行分析。

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
