# AGENTS.md

本文件适用于当前项目根目录及其所有子目录。

## 项目架构

项目由以下三个职责独立的模块组成：

1. Python Pipeline：负责抓取、清洗和写入数据。
2. Next.js App：负责读取并展示数据。
3. PostgreSQL：负责持久化保存数据。

开发时应保持模块边界清晰，通过明确的数据结构或接口协作，不得把抓取、展示和存储职责混杂在同一模块中。新增实现前应先复用项目已有的模块、接口和工具。

## Python 虚拟环境

- 所有 Python 相关的安装、运行、测试和工具命令必须使用 `pipeline/.venv`。
- Pipeline 要求 Python 3.12；如果虚拟环境不存在，先执行
  `python3.12 -m venv pipeline/.venv` 创建它。
- 依赖统一由 `pipeline/requirements.txt` 管理，使用
  `pipeline/.venv/bin/python -m pip install -r pipeline/requirements.txt` 安装。
- 优先直接使用 `pipeline/.venv/bin/python`，或激活 `pipeline/.venv` 后再执行命令。
- 禁止向系统 Python 环境安装项目依赖。

## 异步优先

- 抓取、HTTP 请求、数据库访问、文件 I/O 和跨模块调用等 I/O 密集路径应尽可能采用异步架构。
- 异步代码应使用与现有技术栈兼容的异步客户端和连接池，避免在事件循环中执行阻塞 I/O。
- 不为纯 CPU 计算或没有并发收益的简单流程强行引入异步；确需执行阻塞操作时，应明确隔离，避免阻塞主事件循环。

## 开发要求

- 接口、框架和依赖的用法必须先查阅项目代码或官方文档，不得凭猜测实现。
- 需求或业务规则不明确时，应先确认再修改。
- 修改应保持小步、聚焦，并补充与变更风险相匹配的校验和测试。
