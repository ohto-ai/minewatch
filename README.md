# MC Server Log Fetcher & Viewer

从远程 API 拉取 Minecraft 服务器日志，存入 SQLite，并通过 Flask Web 界面实时展示。

## 架构

```
main.py         入口，启动轮询循环
fetcher.py      从远端 API 拉取日志，清洗 ANSI 转义码
auth.py         JWT 认证与 Token 自动续期
db.py           SQLite 读写（WAL 模式，去重写入）
schedule.py     动态轮询间隔（夜间变慢，周末变快）
config.py       配置（环境变量 / .env）
server.py       Flask Web 查看器（独立运行）
```

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env，填入账号密码
```

环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `MC_BASE_URL` | API 地址 | `http://xcon.top:8585` |
| `MC_USERNAME` | 登录账号 | — |
| `MC_PASSWORD` | 登录密码 | — |

### 2. 启动日志采集

```bash
pip install requests
python main.py
```

采集器会持续运行，按调度策略轮询远端 API，新日志自动存入 `logs.db`。

**动态轮询策略**（见 `schedule.py`）：

| 时段 | 间隔 |
|---|---|
| 夜间 02:00–07:59 | 每 180s |
| 工作日 | 每 60s |
| 周末 | 每 10s |

### 3. 启动 Web 查看器

```bash
pip install flask
python server.py
```

浏览器打开 `http://localhost:5000`。

## Web 界面功能

- **实时增量更新** — AJAX 轮询，只拉取新消息，不刷新整页
- **子服筛选** — 按服务器名称过滤
- **关键词搜索** — 模糊匹配日志内容
- **查询任务队列** — 在 Web 创建关键词任务，由 fetcher 排队执行并回填日志库
- **时间范围** — datetime-local 起止筛选
- **隐藏堆栈** — 仅显示标准日志头行
- **分页** — 50/100/200 条/页可调
- **玩家高亮** — 自动识别玩家名、加入/离开事件
- **ANSI 清洗** — 去除终端颜色码，展示纯文本

## API 端点

| 路径 | 说明 |
|---|---|
| `GET /` | Web 界面 |
| `GET /api/logs` | JSON 格式日志（分页） |
| `GET /api/stats` | 统计信息（总数、各子服分布） |
| `GET /api/poll` | 增量轮询，参数 `since_time` & `since_id` |
| `GET /api/query_tasks` | 查询任务列表（队列/执行中/完成/失败） |
| `POST /api/query_tasks` | 创建查询任务，JSON: `{"keyword":"..."}` |

## 数据库

单文件 SQLite `logs.db`，WAL 模式支持读写并发（采集器写、Web 查看器读互不阻塞）。

```sql
logs (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    log   TEXT    NOT NULL,
    name  TEXT    NOT NULL,
    time  INTEGER NOT NULL,
    UNIQUE(time, log)
)
```
