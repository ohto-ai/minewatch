# MC Server Log Fetcher & Viewer

从远程 API 拉取 Minecraft 服务器日志，存入 SQLite，并通过 Flask Web 界面实时展示。

## 架构

```
main.py         入口，启动轮询循环
backfill_time_keywords.py  服务器端批量回填关键词脚本
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
| `MC_QUERY_TASK_STEP_INTERVAL` | 查询任务串行执行间隔（秒） | `1` |

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

### 4. 服务器端补抓旧日志

当远端接口单次只返回 100 条日志时，可以先把“全天每分钟”的时间字符串批量加入查询队列，再由采集器慢慢回填历史消息：

```bash
python backfill_time_keywords.py
python main.py
```

默认会创建 `00:00` 到 `23:59` 的 1440 个关键词任务；重复执行会跳过已存在任务，并自动重试之前失败的任务。
当某个“分钟关键词”实际抓到 100 条上限时，采集器还会自动细化为 `HH:MM:SS` 的 60 个秒级任务继续补抓。

如果只想补抓部分时段，也可以限制小时范围：

```bash
python backfill_time_keywords.py --from-hour 8 --to-hour 12
```

## Web 界面功能

- **实时增量更新** — AJAX 轮询，只拉取新消息，不刷新整页
- **子服筛选** — 按服务器名称过滤
- **关键词搜索** — 模糊匹配日志内容
- **查询任务队列** — 在 Web 创建关键词任务，由 fetcher 排队执行并回填日志库
  - 同一调度周期内会按队列顺序逐个执行全部待处理任务
  - 任务间隔可通过 `MC_QUERY_TASK_STEP_INTERVAL` 控制，且不会超过当前调度间隔
- **单向数据库同步** — 在 Web 中填写另一台 Minewatch 的地址，后台按游标批量拉取远端日志并写入本地库
  - 使用远端 `(time, id)` 游标顺序导出，不依赖两边数据库的本地自增 ID 一致
  - 本地仍通过 `UNIQUE(time, log)` 去重，适合两个库内容大量重合的场景
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
| `GET /api/sync_tasks` | 数据库同步任务列表（需登录） |
| `POST /api/sync_tasks` | 创建同步任务（需管理员权限），JSON: `{"remote_url":"http://host:5000"}` |
| `GET /api/logs/export` | 供另一台 Minewatch 增量拉取日志（登录或 `X-Sync-Token`/Bearer），参数 `after_time` & `after_id` |

## 单向数据库同步方案

1. 在**目标库**对应的 Web 页面右侧“数据库同步”中填写**源库**的 Minewatch 地址，例如 `http://10.0.0.8:5000`
2. 点击“开始同步”后，服务端会创建后台同步任务
3. 任务通过源库的 `/api/logs/export` 接口按 `(time, id)` 升序分页拉取数据
4. 每批数据写入目标库时，依旧走本地 `UNIQUE(time, log)` 去重，因此即使两个库的 `id` 完全不同，也能安全合并重叠内容

该流程是**单向**的：只会把远端库的数据补到当前库，不会反向修改远端服务器。

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
