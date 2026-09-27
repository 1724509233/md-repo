# 局域网多设备 Markdown 文档同步 — 设计文档

## 概述

在局域网内实现 Markdown 文档的多设备共享。Win PC 作为服务端，Mac 通过 Python 客户端脚本双向同步，手机通过 Web 页面只读查阅和下载。

**核心场景：** Win PC 上写文档 → Mac 客户端轮询同步到服务端 → 手机浏览器从服务端查看/下载。

## 各端角色

| 设备 | 角色 | 同步方式 |
|---|---|---|
| Win PC | 服务端 + 编辑 | VS Code 直接编辑 vault 目录 |
| Mac | Python 客户端 | 定时轮询，双向增量同步 |
| 手机 (Android) | Web 只读 | 浏览器打开 Web 页面，阅读 + 下载单个 .md |

## 架构

```
┌──────────────────────────────────────────────────┐
│  Win PC (Server)                                  │
│                                                    │
│  FastAPI (port 9888, uvicorn)                     │
│  ├── /api/sync         POST   同步端点 (Mac 用)    │
│  ├── /api/docs         GET    文档元数据列表       │
│  ├── /api/docs/{path}  GET    下载单个文件         │
│  ├── /api/docs/{path}  POST   上传单个文件 (Mac)   │
│  ├── /api/health       GET    健康检查             │
│  └── /                 GET    Web 页面 (手机用)    │
│                                                    │
│  vault/ (数据目录 = Obsidian vault)                │
│  ├── notes/2026/09/xxx.md                         │
│  └── .sync/                                       │
│      ├── index.db    (SQLite: 文件哈希索引)        │
│      └── logs/       (集中日志, 30天保留)          │
└──────────────────────────────────────────────────┘
         ▲                       ▲
         │ HTTP (局域网)         │ HTTP (局域网)
         │                       │
┌────────┴───────┐      ┌───────┴──────┐
│  Mac Client    │      │ 手机浏览器    │
│  Python 脚本   │      │ Web 只读页面  │
│  vault/        │      │ (无本地副本)  │
│  .sync/state   │      │              │
└────────────────┘      └──────────────┘
```

## 同步协议

### 同步流程（Mac 客户端 → 服务端）

1. 客户端扫描本地 vault，计算所有 `.md` 文件的 SHA-256 哈希
2. 客户端 POST `/api/sync`，提交：
   - `device_name`: 设备标识（如 `"mac"`）
   - `local_files`: 本地文件清单 `{path: hash}`
   - `logs`: 本次同步的日志数组 `[{level, message, timestamp}]`
3. 服务端对比本地清单和服务端索引，返回：
   - `to_pull`: 服务端有但客户端没有/过期的文件路径列表
   - `to_push`: 客户端有但服务端没有/过期的文件路径列表
   - `conflicts`: 双方都修改过的文件路径列表
4. 客户端按 `to_pull` 逐个 GET `/api/docs/{path}` 下载
5. 客户端按 `to_push` 逐个 POST `/api/docs/{path}` 上传
6. 冲突文件：客户端下载服务端版本，保存为 `xxx.conflict-{device_name}.md`，本地版本保留

### 并发控制

- 服务端维护一个同步锁（内存变量），同一时间只接受一个客户端的 `/api/sync` 请求
- 锁超时 5 分钟自动释放
- 获取锁失败返回 HTTP 409，客户端下次轮询重试

### 增量判断

- 服务端 SQLite 索引表：`path TEXT PRIMARY KEY, hash TEXT, mtime REAL`
- 对比逻辑：hash 不同 = 文件变化；path 只在一侧 = 新增
- **删除不同步**：客户端删除的文件不出现在 `local_files` 中，服务端不主动删除

### 首次同步

- 客户端 `.sync/state.json` 不存在时，客户端仍扫描本地 vault 计算哈希，提交实际文件清单
- 服务端对比后：服务端有而客户端没有的文件 → `to_pull`；客户端有而服务端没有的文件 → `to_push`
- 效果：双向全量同步，双方文件合并到服务端

### 轮询

- Mac 客户端每 5 分钟（300 秒）执行一次同步
- 网络异常时记录 WARNING 日志，下次轮询重试

## API 设计

| 端点 | 方法 | 请求体 | 响应 |
|---|---|---|---|
| `/api/sync` | POST | `{device_name, local_files: {path: hash}, logs: [{level, message, timestamp}]}` | `{to_pull: [path], to_push: [path], conflicts: [path]}` |
| `/api/docs` | GET | — | `[{path, hash, mtime}]` |
| `/api/docs/{path}` | GET | — | 文件内容 (text/markdown) |
| `/api/docs/{path}` | POST | multipart: file | `{path, hash}` |
| `/api/health` | GET | — | `{status: "ok", file_count: N}` |
| `/` | GET | — | Web 页面 (HTML) |

- `{path}` 是 URL 编码的相对路径（如 `notes%2F2026%2F09%2Fxxx.md`）
- 错误响应：`{error: "message"}`，HTTP 状态码 404/409/500

## Web 页面（手机只读）

- 文档树：按文件夹层级展示所有 .md 文件
- 点击文件 → Markdown 渲染阅读（客户端 JS 渲染，如 marked.js）
- 下载按钮：下载单个 .md 文件（触发浏览器下载）
- 无编辑、无上传、无删除、无新建
- 纯静态 HTML + JS，由 FastAPI 直接 serve

## 项目结构

```
md-repo/
├── pyproject.toml              # 项目配置 + 依赖
├── server/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app 入口 + uvicorn 启动
│   ├── config.py               # 配置加载 (config.yaml)
│   ├── db.py                   # SQLite 索引操作
│   ├── sync.py                 # 同步逻辑 (对比、冲突检测)
│   ├── api.py                  # API 路由
│   ├── web.py                  # Web 页面 (HTML/JS serve)
│   └── logrotate.py            # 日志清理 (每天定时)
├── client/
│   ├── __init__.py
│   ├── main.py                 # 客户端入口 (轮询循环)
│   ├── config.py               # 配置加载
│   ├── sync.py                 # 同步逻辑 (哈希计算、上传下载)
│   └── state.py                # 本地状态管理 (.sync/state.json)
├── tests/
│   ├── test_sync.py            # 同步逻辑测试
│   ├── test_api.py             # API 端点测试
│   └── conftest.py             # 共享 fixtures
├── config.example.yaml         # 配置示例
└── README.md
```

## 依赖

| 用途 | 包 |
|---|---|
| 服务端 Web 框架 | fastapi |
| 服务端 ASGI 服务器 | uvicorn |
| 配置解析 | pyyaml |
| 客户端 HTTP | httpx |
| 测试 | pytest |

Python 版本：3.10+

## 配置

```yaml
# config.yaml (各端一份)

# 服务端配置 (Win PC)
server:
  host: 0.0.0.0
  port: 9888
  vault_dir: ./vault

# 客户端配置 (Mac)
client:
  server_url: http://192.168.1.100:9888
  vault_dir: ./vault
  device_name: mac
  poll_interval: 300    # 秒
```

## 错误处理 & 日志

### 错误处理

| 场景 | 处理 |
|---|---|
| 网络断开 | 客户端捕获异常，记录 WARNING，下次轮询重试 |
| 服务端锁占用 | 客户端收到 409，记录 INFO，下次轮询重试 |
| 文件读写失败 | 记录 ERROR，跳过该文件，继续同步其他文件 |
| 路径不存在 | 返回 404 |

### 日志

- 级别：INFO / WARNING / ERROR（三级）
- 格式：纯文本 `2026-09-27 23:30:00 [INFO] message`
- 客户端日志：本地 `.sync/logs/sync.log` + 同步时上报服务端
- 服务端日志：`vault/.sync/logs/server.log` + 各客户端上报日志（按设备名分文件）
- 保留：30 天，服务端每天凌晨 3 点清理过期日志

## 测试策略

pytest 核心路径覆盖：

- **test_sync.py**：哈希对比逻辑、冲突检测、首次同步、增量同步
- **test_api.py**：各端点正常响应、404/409 错误、锁竞争
- **conftest.py**：临时 vault 目录、测试 FastAPI app、mock HTTP

不测：网络异常模拟、并发压力、日志清理定时任务

## 范围外（YAGNI）

- 无认证/HTTPS
- 无公网访问/内网穿透
- 无版本历史
- 无删除同步
- 无 Web 编辑/上传
- 无手机端本地副本/离线同步
- 无附件（图片等）同步，仅 .md
- 无文档元数据/标签索引（索引仅供内部同步用）
- 无开机自启
- 无代码规范工具强制
