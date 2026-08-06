# 部署说明

本项目由 PostgreSQL、Python Pipeline 和 Next.js Web 三部分组成：Pipeline 负责抓取和写入内容，Web 负责展示并维护频道配置。

## 环境要求

- PostgreSQL、`psql`、`sha256sum`
- Python 3.12
- Node.js 20.9 或更高版本
- 运行 Pipeline 时需要已登录的 Codex CLI 和有效的 YouTube Cookie

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

将 YouTube Cookie 写入 `pipeline/config/youtube.cookies.txt`，并限制文件权限：

```bash
chmod 600 pipeline/config/youtube.cookies.txt
```

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

运行一次 Pipeline：

```bash
pipeline/.venv/bin/python pipeline/main.py run
```

启动 Web 服务：

```bash
cd web
npm run start -- --hostname 0.0.0.0 --port 3000
```

Web 必须通过 `/<密钥>` 访问，根路径 `/` 返回 404。反向代理必须原样转发完整路径，不得
剥离密钥前缀；健康检查也应请求带密钥的路径。该路由密钥适用于内部访问约束，不替代鉴权，
不要在文档、日志或公开配置中记录实际密钥。

生产环境中应使用外部调度器周期运行 Pipeline，并使用进程管理器保持 Web 服务常驻。
`run` 和 `video` 通过 PostgreSQL advisory lock 防止任务重叠；调度器触发重叠任务时，后者
会立即失败且不会排队。若需要在 Web 中新增频道，必须保留 `pipeline/.venv` 和私有
YouTube Cookie；仅作只读展示时可跳过 Pipeline 的安装和运行。
