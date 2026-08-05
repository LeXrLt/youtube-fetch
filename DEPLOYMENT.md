# 部署说明

本项目由 PostgreSQL、Python Pipeline 和 Next.js Web 三部分组成：Pipeline 负责抓取和写入内容，Web 负责展示并维护频道配置。

## 环境要求

- PostgreSQL、`psql`、`sha256sum`
- Python 3.12
- Node.js 20.9 或更高版本
- 运行 Pipeline 时需要已登录的 Codex CLI 和有效的 YouTube Cookie

## 配置

在项目根目录创建 `.env`：

```dotenv
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password
POSTGRES_DB=youtube_fetch
```

生产环境可另行提供最小权限的频道管理账号：

```dotenv
CHANNEL_ADMIN_POSTGRES_USER=channel_admin
CHANNEL_ADMIN_POSTGRES_PASSWORD=channel_admin_password
```

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

生产环境中应使用外部调度器周期运行 Pipeline，并使用进程管理器保持 Web 服务常驻。若需要在 Web 中新增频道，必须保留 `pipeline/.venv` 和私有 YouTube Cookie；仅作只读展示时可跳过 Pipeline 的安装和运行。
