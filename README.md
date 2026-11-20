# 内网 APT 源（FastAPI）

单发行版、单组件、单架构的 APT 软件源。每次发布先落盘、后切换
`Release`，启用 **Acquire-By-Hash**，并只保留最近两次成功发布所引用的
索引和安装包。拿到旧 `Release` 的客户端仍可按其中的 SHA-256 取回旧索引，
再按索引校验下载安装包；同名同版本重新构建也互不覆盖。

## 快速开始（docker compose）

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
```

上传一批 `amd64` 的 `.deb`（整版发布，一次请求一批）：

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/publish \
  -F files=@hello-internal_1.0.0_amd64.deb \
  -F files=@libfoo_2.1.0_amd64.deb | python3 -m json.tool
```

也可以直接用浏览器打开 <http://127.0.0.1:8080/docs> 的上传表单
（OpenAPI 文档在 `/swagger-ui`）。

## 客户端配置与实测

```bash
sudo ./clients/setup-client.sh http://127.0.0.1:8080 stable main
# 等价于写入 /etc/apt/sources.list.d/internal.list：
#   deb [trusted=yes arch=amd64] http://127.0.0.1:8080/ stable main

sudo apt-get update
apt-get download hello-internal          # 下载并校验
sudo apt-get install hello-internal      # 或直接安装
```

仓库未签名，因此用 `trusted=yes`。若要签名，增加 `Release.gpg`/`InRelease`
并把 `trusted=yes` 换成 `signed-by=/usr/share/keyrings/internal.gpg`。

一键端到端验证（发布两版、`apt-get update`/`download`、旧 Release 客户端
按哈希取包、第三版触发淘汰）：

```bash
sh scripts/e2e-apt.sh http://127.0.0.1:8080
```

实测输出示例：

```
== release 1 published (index 71440bc1...)
== apt-get download smoke-one -> smoke-one_1.0.0_amd64.deb
== release 2 published (index 6fc51fd9...)
== apt-get download smoke-two -> smoke-two_2.0.0_amd64.deb
== previous release index and packages verified via by-hash
== retention keeps exactly the two most recent releases
== OK
```

## 发布语义

| 需求 | 实现 |
| --- | --- |
| 新版编制期间继续供包 | 新索引/包先写入不可变路径（`by-hash/SHA256/<hash>`、内容寻址的 pool 文件），旧 `Release` 与旧索引在此期间保持不变 |
| 切换时索引与包同时可下载 | `Release` 用原子 `rename` 作为唯一提交点；在此之前所有被引用的文件都已 fsync 落盘 |
| 旧 Release 客户端仍可取旧索引 | `Acquire-By-Hash: yes` + `dists/stable/main/binary-amd64/by-hash/SHA256/<sha256>`，`dists/stable/by-hash/SHA256/<sha256>` 亦提供 Release 本体 |
| 同名同版本重发 | pool 文件名带内容哈希：`<pkg>_<ver>_<arch>_<sha16>.deb`，两版并存 |
| 只开放最近两次成功发布 | GC 以 journal 中最新 2 条 `committed` 记录为根，删除其余 by-hash 索引与 pool 文件 |
| 坏包 / 提交前中断两版照旧 | 上传先完整校验；任一文件非法则整批拒绝。提交中断时旧 `Release` 不变，重启后 `reconcile()` 回滚 |
| 重启后仍可取用 | 启动时对比磁盘上 `Release` 的哈希与 journal 中 `staged` 记录：一致则补记为 `committed`，否则标记 `aborted` 并清理 |

### 目录结构（数据卷内）

```
/data
├── export/                                  # 只读 HTTP 根目录
│   ├── dists/stable/Release
│   ├── dists/stable/by-hash/SHA256/<release-sha256>
│   ├── dists/stable/main/binary-amd64/Packages(.gz)
│   ├── dists/stable/main/binary-amd64/by-hash/SHA256/<index-sha256>
│   └── pool/main/<bucket>/<pkg>/<pkg>_<ver>_<arch>_<sha16>.deb
├── incoming/<token>/                        # 上传暂存，发布后删除
└── state.sqlite3                            # 发布 journal（GC 的唯一依据）
```

`Release` 同时列出 `Packages` 与 `Packages.gz`：APT 2.6 以未压缩名识别
索引目标，只列 `Packages.gz` 会被静默跳过（本项目最初即踩此坑）。

## 双进程 /pool/ 下载与共享限速

`apt-repo` 仍是唯一发版入口；在此之上，`docker-compose.yml` 还启动两个
**相互独立**的下载进程 `pool-a`（宿主端口默认 `8081`）与 `pool-b`
（默认 `8082`）。它们以只读方式挂载同一数据卷，直接对外提供 `/pool/`
下的 `.deb` GET，两处均可独立访问：

```bash
curl -O http://127.0.0.1:8081/pool/main/s/smoke-one/smoke-one_1.0.0_amd64_<sha16>.deb
curl -O http://127.0.0.1:8082/pool/main/s/smoke-one/smoke-one_1.0.0_amd64_<sha16>.deb
```

两处共用**同一个字节速率上限**（Compose 里的 `APT_REPO_RATE_BPS`，默认
4 MiB/s），由共享卷 `apt-repo-limiter` 上的令牌桶状态协调
（`app/ratelimit.py`：`flock` 保护的 `state.json`，跨进程读写）：

| 语义 | 实现 |
| --- | --- |
| 任意时段内，两处提交给连接的包正文字节合计 ≤ 速率×时长 + 64 KiB | 全局令牌桶：容量 `APT_REPO_RATE_BURST`（默认 65536），按 `APT_REPO_RATE_BPS` 匀速补充；每个 32 KiB 分片写入连接前先从全局桶取票 |
| 有正文可发的来源 IP 平分带宽 | 每个活跃来源 IP 一个公平桶，补充速率 = 上限 ÷ 活跃 IP 数 |
| 同一 IP 连到两处也只占一份 | 公平桶按来源 IP  keyed，与该 IP 的连接数、所连进程无关 |
| 只有一个来源持续接收时可用足上限 | 活跃 IP 数为 1 时，其公平桶补充速率即全量上限 |
| 强杀一处后另一处在途下载继续，2 秒内可用空出的份额，共享上限始终生效 | 限流状态存在共享卷而非进程内存，进程死亡不影响幸存方；来源 IP 超过 `APT_REPO_RATE_ACTIVE_TTL`（默认 1 s）未取票即失去份额，幸存连接随即提速 |

pool 服务的环境变量（`pool-a`/`pool-b` 必须一致，Compose 已统一注入）：

| 环境变量 | 默认值 |
| --- | --- |
| `APT_REPO_RATE_BPS` | `4194304`（4 MiB/s） |
| `APT_REPO_RATE_BURST` | `65536`（64 KiB） |
| `APT_REPO_RATE_ACTIVE_TTL` | `1.0`（秒） |
| `APT_REPO_LIMITER_DIR` | 容器内 `/limiter`（共享卷） |
| `APT_REPO_POOL_PORT` | 容器内 `8081`；宿主映射 `APT_POOL_A_PORT`/`APT_POOL_B_PORT`（默认 `8081`/`8082`） |

一键验证共享限速语义（发布三个大包 → 单源吃满 → 同源跨进程双连仍占一份
→ 双源平分 → SIGKILL 一处后另一处在途下载继续且 2 秒内接管份额 →
核对每个成功下载的 SHA-256 与全程字节上界）：

```bash
scripts/verify-shared-limit.sh          # 需要 docker compose v2
KEEP=1 scripts/verify-shared-limit.sh   # 保留现场便于排查
```

脚本使用独立的 compose 项目（默认名 `aptrepo-limit-verify`，端口
18080–18082）与两个客户端容器（两个来源 IP），不影响正常部署，结束后
自动 `down -v` 清理。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/publish` | multipart 字段 `files`，一次一批，返回 201 与发布记录 |
| GET | `/api/status` | 当前配置、最新发布、发布历史 |
| GET | `/api/publications` | 发布历史（含 `aborted`） |
| GET | `/docs` | 上传表单 |
| GET | `/swagger-ui` | OpenAPI 文档 |
| GET/HEAD | `/dists/...`、`/pool/...` | 仓库文件（只读） |

错误语义：包非法 / 重复的同名同版本 / 批次超限返回 `400`/`413`，且
仓库状态完全不变。

## 配置

| 环境变量 | 默认值 |
| --- | --- |
| `APT_REPO_DATA_DIR` | `./data`（容器内 `/data`） |
| `APT_REPO_DISTRIBUTION` | `stable` |
| `APT_REPO_COMPONENT` | `main` |
| `APT_REPO_ARCHITECTURE` | `amd64` |
| `APT_REPO_ORIGIN` / `APT_REPO_LABEL` | `Internal APT Repository` |
| `APT_REPO_RETENTION` | `2` |
| `APT_REPO_MAX_FILE_BYTES` | `536870912` |
| `APT_REPO_MAX_BATCH_FILES` / `_BYTES` | `500` / `4 GiB` |

## 操作约束

- **单写入进程**：发布通过进程内线程锁 + `flock` 串行化，`docker compose`
  固定 `--workers 1`。多副本部署时需改为共享存储上的分布式锁。
- **限速器状态在共享卷**：`pool-a`/`pool-b` 通过 `apt-repo-limiter` 卷上的
  `state.json`（`flock` 保护）协调全局令牌桶；两处的 `APT_REPO_RATE_*`
  必须一致。状态文件损坏时自动重建（重建瞬间最多多放行一个 burst）。
- **unsigned**：默认不签名；生产环境应补 `InRelease`/`Release.gpg`。
- **下载与 GC 竞态**：响应通过已打开的 fd 流式发送，即使文件随后被 GC
  删除，进行中的下载仍能读到完整内容（POSIX inode 语义）。
- 容器以只读根文件系统、非 root（uid 10001）、`no-new-privileges` 运行。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload          # 数据写入 ./data
pytest -q                              # 22 项发布测试 + 限流器/pool 服务测试
```

测试覆盖：发布/回读、旧 Release 按哈希取旧索引与旧包、同名同版本重发、
两版保留与第三版淘汰、坏包整批拒绝、错误架构拒绝、提交前中断回滚、
提交后崩溃恢复、下载中被 GC、Release 格式与 by-hash 服务、路径穿越；
共享限流器的速率上限/按 IP 平分/跨进程一致性与状态自愈、pool 服务的
路径防护与限速响应。
