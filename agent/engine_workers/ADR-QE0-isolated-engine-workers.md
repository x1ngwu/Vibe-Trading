# ADR QE0：隔离量化引擎 worker 的 PoC 边界

- 状态：协议与两平台 direct smoke 已通过；post-commit review 的 CT-04、WK-02 和实际执行源码闭包
  已修复并补齐负例，WK-01/SE-04 制品完整性、snapshot 内容绑定及内核隔离仍未关闭
- 日期：2026-07-23 JST
- Vibe 源码基线：`6bccd6974ad7d7a63318bce6c41f0064614404d4`
- 源码分支：`agent/nlq-quant-engine-poc`
- QUANTAXIS：`a69e978a2e38d045a64c380cc3b5c9fa08fa4903`（MIT，2.1.0a2）
- vn.py：`1b78494979deb4c4996f6b864f234d9839f2f239`（MIT，4.4.0）

## 决策

Vibe 主进程只持有引擎中立、严格版本化的 JSONL/stdin 协议。每个请求启动一个独立进程，worker
的 stdout 只允许一条换行结尾的 JSON；第三方库的普通输出被重定向到 stderr。runner 校验固定引擎
commit、请求内容哈希、响应关联、超时、取消和输出上限，并终止完整进程组。

两个引擎使用各自的 Python 环境和 lock，不进入 Vibe 的 `requirements-lock.txt`：

- QUANTAXIS 固定 Python 3.11。完整 lock 在 Python 3.12 会被 `alphalens==0.4.0` 的旧构建脚本阻断；
  Python 3.11.15 下 170 个包可以解析和安装。聚合包顶层 import 会加载 DB/Web/Jupyter，因此 PoC 从
  固定安装包直接加载经 SHA-256 校验的叶子源模块，不执行 `QUANTAXIS/__init__.py`。
- vn.py PoC 使用 Python 3.12；它只作为 CI/诊断 oracle 候选，不进入首版生产镜像。
- QE0 不注册 API、tool、后台任务、HTTP 端口、volume 或生产部署入口。

worker 收到显式 allowlist 环境：固定 `Asia/Shanghai`、`C.UTF-8`、hash seed 和单线程变量；不继承
API key、token、password 等秘密。Python socket 的 connect、DNS、bind 和 UDP 发送入口在加载
外部引擎前被禁用，父进程的动态加载路径不继承。snapshot 路径在 runner 和 worker 两侧拒绝越界、
symlink、FIFO/device 等特殊文件。

上述 Python 网络 guard 和路径检查是 PoC 防线，不是生产安全边界。当前主机无法创建 user/network
namespace：bubblewrap 分别失败于 `setting up uid map: Permission denied` 和
`loopback: Failed RTM_NEWADDR: Operation not permitted`。因此“内核级无网络”和“只读 bind mount”
尚未验证；在容器/seccomp/namespace 门禁完成前，不得把 worker 接入生产。

## adopt / port / reimplement / drop

| 平台/模块 | 决策 | QE0 依据与后续边界 |
|---|---|---|
| Vibe JSONL protocol/runner | reimplement | 契约、哈希、错误码、资源限制和进程生命周期必须由 Vibe 控制。 |
| QUANTAXIS 顶层包 | drop（正式 adapter） | 顶层 import 拉入 Fetch、Mongo、ClickHouse、Web、Jupyter/pyfolio 等，与离线最小 worker 冲突。只保留审计/PoC。 |
| `QAData.data_fq` | port 候选 | 固定源模块 direct smoke 已通过 qfq/hfq；仍须在 QE2 用公式与真实公司行动 fixture 验证。 |
| `QAIndicator` / `QAFactor` 纯计算 | adopt 候选 | 固定源模块 MA2 smoke 已通过；只接无 I/O、可固定输入的 operation，并逐因子 capability-gate。 |
| `trade_date_sse` 内置常量 | drop | 不能作为点时数据真值；交易日历由固定 snapshot 提供，QUANTAXIS 只可作抽样比较。 |
| `QIFI_Account` | port/对照候选 | 可作最小账户 smoke；A 股 T+1、整手、涨跌停、费用和公司行动语义未验证前不能作为正式账本。 |
| QUANTAXIS Fetch/DB/Web/Jupyter/PubSub | drop | worker 禁止联网和外部数据库；这些模块扩大依赖和攻击面。 |
| vn.py `EventEngine` | adopt（oracle） | 合成订单/成交事件已完成异步投递 smoke；只用于测试/诊断。 |
| vn.py order/trade objects and enums | adopt（oracle） | 可作为 QE6 独立事件重放输入层，不成为用户可见契约。 |
| vn.py backtesting/gateway | port 候选 | 等 EngineRequest 和 A 股规则表稳定后再映射；QE0 不启用模拟盘/实盘 gateway。 |
| vn.py PySide6/chart/GUI | drop | 与 headless worker 无关，虽被上游核心依赖带入，生产镜像不打包。 |

## 锁文件和许可证证据

| 文件 | SHA-256 |
|---|---|
| `quantaxis/requirements.in` | `2baf884cd7b2b12223d15dcb7880e760273685f21b0dd7a3738e8f6bbadf023e` |
| `quantaxis/requirements-lock.txt` | `d81da4ce671312408a00c3c544b7e0a7c0c09ff849e9d4365a33fa815e734b93` |
| `vnpy/requirements.in` | `a31c048b40d5c23bfcfdc4d03ea14b3c1dc5217e8c0b4859b3e809cc68c5921a` |
| `vnpy/requirements-lock.txt` | `540df31956cd6e5a2c154174a60d7585dfc0b41c3bda4d30f256a4922932632f` |

各 worker 的 `NOTICE.md` 记录上游 URL、commit、版本、MIT 许可证和许可证原文链接；没有复制第三方
源码到 Vibe。lock 是 QE0 的 Linux/Python 环境冻结，不修改主 lock。capability 和 direct smoke 会先
校验精确安装版本，再校验 QUANTAXIS 8 个实际加载源文件和 vn.py 7 个实际 import/执行文件的
SHA-256；任一文件缺失、为 symlink 或内容漂移均以 `ENGINE_PROVENANCE_MISMATCH` 失败，缺包和错版
分别以 `ENGINE_UNAVAILABLE`、`ENGINE_VERSION_MISMATCH` 失败，不再宣告可执行 operation。

post-commit 修复已纳入遗漏的 `QAUtil/QAParameter.py` 与 vn.py 4 个包初始化/导入文件，并逐一覆盖
15 个受信文件的 tamper 负例。当前 lock 仍只固定版本/VCS commit，没有 wheel/sdist artifact hash；
因此执行源码闭包已关闭，但 WK-01/SE-04 的依赖制品完整性仍为 partial。

## 2026-07-22–23 测量

测量主机：Linux x86_64，3.6 GiB RAM；环境位于 `/tmp`，缓存和下载体积不计入源码。
数字是 PoC 观测值，不是性能 SLA；“冷启动”指新 worker 进程，不代表清空宿主页缓存。

| 项目 | 冷启动/耗时 | 峰值 RSS | 环境磁盘增量 | 结论 |
|---|---:|---:|---:|---|
| QUANTAXIS capabilities | 0.122 s | 23,232 KiB | 1.1 GiB | 通过 |
| QUANTAXIS security probe | 0.079 s | 23,040 KiB | 同上 | 通过 |
| QUANTAXIS direct smoke | 0.892 s | 156,412 KiB | 同上 | 通过 |
| vn.py capabilities | 0.101 s | 21,332 KiB | 932 MiB | 通过 |
| vn.py security probe | 0.060 s | 17,656 KiB | 同上 | 通过 |
| vn.py EventEngine direct smoke | 0.098 s | 19,572 KiB | 同上 | 通过 |
| vn.py 全新 lock 安装 | 42.87 s | 145,728 KiB | 932 MiB | 通过；安装后 direct smoke 1.03 s |

QUANTAXIS Python 3.11 lock 安装解析/构建约 47 秒（已有网络和共享缓存）；vn.py 在全新 venv、
固定 lock、已有网络和共享缓存下安装为 42.87 秒，随后 direct smoke 通过。聚合包顶层 import 的
先前堆栈显示其加载 `QAWebServer → QIFI → pyfolio → IPython`，并创建 PyMongo periodic executor 线程；因此该入口继续
判定为 `drop`。新的固定叶子模块边界完成 qfq/hfq、内置日历抽样、MA2 与 QIFI 离线最小成交 smoke。
QIFI 的未触发 persistence/save 导入使用显式惰性占位，因此只证明 `port/对照候选`，不证明正式
adapter 语义。

## 测试门禁映射

| ID | 状态 | 证据/缺口 |
|---|---|---|
| CT-04 | passed | 两个引擎分别覆盖缺包、错误版本和 provenance 不符；校验失败时 capability 不宣告 operation。 |
| WK-01 | partial | 三套依赖分离且固定版本/VCS commit；wheel/sdist 制品 hash 未固定。 |
| WK-02 | passed | 严格响应解码及完整 response canonical 校验拒绝 result/error 中的 `NaN`、`Infinity`、`-Infinity`。 |
| WK-04 | passed | timeout、cancel 和含子进程的进程组清理自动测试。 |
| WK-05 | partial | 越界、symlink、FIFO 被拒；只读挂载因 namespace 权限尚未验证。 |
| WK-06 | partial | 秘密清除、动态加载路径清除、时区/locale/seed/thread、connect/DNS/bind/UDP guard 通过；内核级断网未验证。 |
| SE-02 | passed（PoC 范围） | 测试环境不继承命名秘密；stderr 有界；仍需合并前执行仓库秘密扫描。 |
| SE-04 | partial | QUANTAXIS 8 文件、vn.py 7 文件执行源码闭包及逐文件 tamper 已通过；依赖制品 artifact hash 尚未固定。 |
| snapshot 内容绑定 | open（QE1/QE2 前置） | `snapshot_sha256` 只校验格式，尚未与文件或 manifest 内容比对。 |
| PR-01 | passed（PoC 范围） | 两平台启动、RSS、磁盘增量和全新 lock 安装耗时已记录。 |
| 两平台 direct smoke | passed | QUANTAXIS 叶子模块 qfq/hfq、日历、MA2、QIFI 离线成交和 vn.py EventEngine 均通过，重复运行核心结果一致。 |

完整 QE0 自动测试命令；隔离解释器未提供时 direct smoke 会明确 skip：

```bash
VIBE_QE0_QUANTAXIS_PYTHON=/path/to/qa-venv/bin/python \
VIBE_QE0_VNPY_PYTHON=/path/to/vnpy-venv/bin/python \
.venv/bin/pytest -q \
  agent/tests/test_quant_engine_protocol.py \
  agent/tests/test_quant_engine_runner.py \
  agent/tests/test_quant_engine_direct_smoke.py
```

post-commit 修复后的定向结果：`53 passed in 3.84s`，使用现有两个隔离解释器完成真实 capability 与
两平台 direct smoke，并覆盖缺包、错版、15 个受信文件逐一篡改及 response result/error 的
`NaN`/`Infinity`/`-Infinity`。修复前的 `24 passed in 4.56s` 仅保留为历史基线。

post-commit 完整后端回归以仓库约定占位值显式跳过实时 Tushare E2E：`5352 passed, 11 skipped,
20 warnings in 174.46s`。skip 均来自实时凭据或环境/门禁条件；warnings 为既有 FastAPI/Starlette
弃用和 pandas `pct_change` future warning，本轮未新增测试失败。

## 后续决策门槛

QE0 不接生产。CT-04 capability fail-closed、WK-02 严格响应 JSON 和实际执行源码闭包已关闭；进入
QE1 前仍须确定 WK-01/SE-04 依赖制品完整性与 snapshot digest 契约，并在具备 user/network namespace
或等价容器/seccomp 的执行环境验证内核级断网、只读 snapshot bind mount 和特殊文件边界。任一剩余项
未关闭时 QE0 G1 保持打开。

QUANTAXIS 叶子模块实验已证明技术可行，但 qfq/hfq、因子和 QIFI 账户仍按 `port/对照候选` 处理；
QE2 必须用固定 fixture、公式和真实公司行动窗口验证，不能直接称为正式 adopt。
