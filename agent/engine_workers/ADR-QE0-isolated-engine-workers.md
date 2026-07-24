# ADR QE0：隔离量化引擎 worker 的 PoC 边界

- 状态：Accepted；协议、两平台 direct smoke、post-commit review、snapshot/制品完整性与容器内核
  隔离均已通过 QE0 G1 门禁。源码提交 `882da2f` 已推送；尚未接生产
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

上述 Python 网络 guard 和路径检查不是单独的生产安全边界。当前主机直接运行 bubblewrap 仍因
`setting up uid map: Permission denied` / `loopback: Failed RTM_NEWADDR` 失败；因此 QE0 改用本机
Docker 的 AppArmor、默认 seccomp 与独立 network/cgroup namespace 验证。容器使用固定本地 image ID、
`--network none --read-only --cap-drop ALL --security-opt no-new-privileges` 和只读 snapshot bind mount；
该门禁已通过，但生产部署尚未新增容器入口。

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
| `quantaxis/requirements-lock.txt` | `5ecdf36d69a2d4f48d8037f2db0d257de9ad11bd5bc808e0d8edcc4b116194b9` |
| `quantaxis/artifact-manifest.json` | `8a01956733a46111c29c40e49bbe31120bfcf05ca00b7cda28ec68d7238a5190` |
| `quantaxis/requirements-artifacts.txt` | `a6acfde9e4b791ef316d50cac3bef901d7f0d14823b774aa2fc496061193313e` |
| `vnpy/requirements.in` | `a31c048b40d5c23bfcfdc4d03ea14b3c1dc5217e8c0b4859b3e809cc68c5921a` |
| `vnpy/requirements-lock.txt` | `540df31956cd6e5a2c154174a60d7585dfc0b41c3bda4d30f256a4922932632f` |
| `vnpy/artifact-manifest.json` | `cdcab8a84e5e4d21f7a18f2649d3b9b4a465e034988afdb6085c7db08b2ab003` |
| `vnpy/requirements-artifacts.txt` | `1f3d8ef3f529ca31bc5cdcf8a04a4870d6b27453a81550a58ab361d088584a5f` |

各 worker 的 `NOTICE.md` 记录上游 URL、commit、版本、MIT 许可证和许可证原文链接；没有复制第三方
源码到 Vibe。lock 是 QE0 的 Linux/Python 环境冻结，不修改主 lock。capability 和 direct smoke 会先
校验精确安装版本，再校验 QUANTAXIS 8 个实际加载源文件和 vn.py 7 个实际 import/执行文件的
SHA-256；任一文件缺失、为 symlink 或内容漂移均以 `ENGINE_PROVENANCE_MISMATCH` 失败，缺包和错版
分别以 `ENGINE_UNAVAILABLE`、`ENGINE_VERSION_MISMATCH` 失败，不再宣告可执行 operation。

post-commit 修复已纳入遗漏的 `QAUtil/QAParameter.py` 与 vn.py 4 个包初始化/导入文件，并逐一覆盖
15 个受信文件的 tamper 负例。真实 wheel 构建另发现 QUANTAXIS lock 漏掉 `setuptools==83.0.0`，
已显式补锁；主 `requirements-lock.txt` 未改变。

## 依赖制品与 snapshot 完整性决策

生产候选采用每个引擎独立的离线 wheelhouse 或不可变 worker 镜像。新增标准库-only
`common/artifact_manifest.py`：把 VCS 依赖解析后的 wheel 与 lock 逐项匹配，manifest 记录 lock
SHA-256、Python/ABI/platform、每个制品的文件名/版本/大小/SHA-256，并生成无 VCS URL 的 hashed
requirements。运行时只允许 `--no-index --require-hashes` 安装或启动已核对 digest 的镜像。

真实证据已经完成：QUANTAXIS 为 CPython 3.11.15、170 个 wheel、316,670,271 bytes；vn.py 为
CPython 3.12.3、44 个 wheel、303,984,263 bytes。两者都在全新 venv 以 `PIP_NO_INDEX=1 --no-index
--require-hashes` 安装成功并通过 direct smoke；分别篡改 `appdirs` 和 `attrs` wheel 时 manifest verifier
以状态 2 失败，恢复后重新验证通过。制品不提交到 Git，只提交 manifest 和 hashed requirements；
生产构建须从受控 artifact store 取得与 manifest 一致的 wheelhouse。

`snapshot.sha256` 现在必填。单文件 digest 是原始字节 SHA-256；目录 digest 是
`vibe.snapshot-manifest.v1` 的 canonical JSON SHA-256，files 按 POSIX 相对路径排序，每项固定
`path/size/sha256`，空目录不影响内容标识。runner 规范化受限路径后校验一次，worker 在 handler
执行前以同一算法再校验；缺失 hash、内容变化、嵌套 symlink 或特殊文件均 fail closed。双侧校验缩小
dispatch 前篡改窗口，但不能替代 WK-05 的内核只读挂载。

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
| Docker kernel-isolation gate | 1.13 s | 256 MiB hard limit | `python:3.11-slim` image | 2 passed；无外网、只读 snapshot、seccomp/no-new-privileges/零 capabilities |

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
| WK-01 | passed（QE0 范围） | 两套真实 manifest/hashed requirements、全新离线 hash 安装、direct smoke 和单制品 tamper 负例通过；主 lock 未改变。 |
| WK-02 | passed | 严格响应解码及完整 response canonical 校验拒绝 result/error 中的 `NaN`、`Infinity`、`-Infinity`。 |
| WK-04 | passed | timeout、cancel 和含子进程的进程组清理自动测试。 |
| WK-05 | passed（QE0 容器范围） | 越界、symlink、FIFO 被拒；只读 bind 的可写对照返回 `EROFS`，worker 可读取并校验 snapshot。 |
| WK-06 | passed（QE0 容器范围） | Python 全 socket guard 与 Docker `--network none` 均通过；seccomp=2、NoNewPrivs=1、CapEff=0，固定环境可重放。 |
| SE-02 | passed（PoC 范围） | 测试环境不继承命名秘密；stderr 有界；仍需合并前执行仓库秘密扫描。 |
| SE-04 | passed（QE0 范围） | 8+7 个执行源码文件和 170+44 个依赖 wheel 均固定 SHA-256；源码与制品 tamper 均失败关闭。 |
| snapshot 内容绑定 | passed（QE0 契约） | hash 必填；file 原始字节、directory v1 canonical manifest 在 runner/worker 双侧校验，篡改失败。 |
| PR-01 | passed（PoC 范围） | 两平台启动、RSS、磁盘增量和全新 lock 安装耗时已记录。 |
| 两平台 direct smoke | passed | QUANTAXIS 叶子模块 qfq/hfq、日历、MA2、QIFI 离线成交和 vn.py EventEngine 均通过，重复运行核心结果一致。 |

完整 QE0 自动测试命令；隔离解释器未提供时 direct smoke 会明确 skip：

```bash
VIBE_QE0_CONTAINER_GATE=1 \
VIBE_QE0_QUANTAXIS_PYTHON=/path/to/qa-venv/bin/python \
VIBE_QE0_VNPY_PYTHON=/path/to/vnpy-venv/bin/python \
.venv/bin/pytest -q \
  agent/tests/test_quant_engine_artifact_manifest.py \
  agent/tests/test_quant_engine_container_isolation.py \
  agent/tests/test_quant_engine_protocol.py \
  agent/tests/test_quant_engine_runner.py \
  agent/tests/test_quant_engine_direct_smoke.py
```

本轮定向结果：`70 passed in 5.20s`，使用两个全新离线安装的隔离解释器与两个短生命周期硬化容器；
除协议、snapshot、manifest 和 artifact tamper 外，还验证只读 bind `EROFS`、无外网、seccomp、
no-new-privileges、零 capabilities 及容器内 worker snapshot 双检。

完整后端回归在不暴露真实 Tushare 凭据的脱敏占位环境执行：`5367 passed, 13 skipped,
20 warnings in 172.93s`。其中 2 个新增 skip 是默认不访问 Docker 的 opt-in 容器门禁；
其余 skip 来自实时凭据或环境条件，warnings 为既有 FastAPI/Starlette
弃用和 pandas `pct_change` future warning，本轮未新增测试失败。

## 后续决策门槛

2026-07-23 JST 在已推送提交 `882da2f` 上完成最终 G1 复核：固定的 QUANTAXIS/vn.py 隔离解释器和
两个短生命周期硬化容器再次通过 `70 passed in 5.72s`。因此 QE0 正式关闭，但仍不接生产。任何正式
adapter/部署都必须复用同等或更强的只读 mount、无网络、seccomp、no-new-privileges 和不可变镜像
约束。

QUANTAXIS 叶子模块实验已证明技术可行，但 qfq/hfq、因子和 QIFI 账户仍按 `port/对照候选` 处理；
QE2 必须用固定 fixture、公式和真实公司行动窗口验证，不能直接称为正式 adopt。

下一实施批次是 QE1（项目第二阶段）：只建立引擎中立公共契约、research store、不可变 fixture/
snapshot 和 golden ledger。QE1 可以定义 adapter 输入输出与对照 fixture，但不新增 QUANTAXIS/vn.py
生产入口；QUANTAXIS 正式基础 adapter 在 QE2 实施，vn.py 独立逐日 oracle 在 QE6 实施。

## 2026-07-24 QE2 第四切片附录

QUANTAXIS 基础 adapter 现提供三个正式、白名单 operation，但仍不注册生产 runner：

- `adjust_prices` 只接受 qfq/hfq 和 `vibe.quantaxis-operation-snapshot.v1`。固定
  `QAData.data_fq` 计算 OHLC，Vibe 按同一 snapshot 的公司行动股数倍率执行首/末日 volume 锚定，
  amount 永不调整；公司行动必须在 `as_of` 已知并落在 snapshot bar 日期。
- `trading_calendar` 的真值始终来自内容绑定 snapshot。固定 `trade_date_sse` 只作 oracle 并显式输出
  mismatch；冻结窗口证明其把 2026-05-04/05 错列为开放日，而 adapter 正确保留 snapshot 的休市事实。
- `compute_factors` 首批只白名单 raw close 上的 MA/EMA，window 限制为 2–512、请求去重；NaN 在协议
  边界规范化为 `null`，同一 snapshot 重放结果一致。

正式 operation 的执行闭包进一步缩为 `QAData.data_fq`、`QAIndicator.base/indicators`、
`QAUtil.QADate_trade/QAParameter` 五个已校验叶子；不会加载 QIFI、QAMarket、Fetch、DB、Web 或顶层包。
主进程 `QuantaxisAdapter` 固定精确 commit，并将每个结果绑定输入 snapshot SHA-256。真实现金+转增窗口
`301336.SZ` 的 qfq 价格、qfq/hfq volume 锚点、amount 不变、MA/EMA 与日历差异均在固定 Python 3.11 /
QUANTAXIS 2.1.0a2 环境通过。

第四切片组合门禁为 `204 passed, 7 skipped in 9.61s`；脱敏占位 Tushare 环境的完整后端为
`5441 passed, 12 skipped, 20 warnings in 180.50s`。这只关闭 QUANTAXIS 基础 operation，不改变
QE0 的 QIFI `port/对照候选` 结论，也不代表生产 runner 已切换；后者属于 QE2 第五切片。

## 2026-07-24 QE2 第五切片附录

生产 runner 现增加显式 opt-in 的 `data_contract=qe2_snapshot`。该路径只读取 run directory 内
内容绑定的 `OfflineDataSnapshot`，要求相对路径无 `..`/symlink、精确 SHA-256、完整 outcome、证券
及顺序、区间、频率、复权方式和字段均与运行配置一致；任何不匹配都失败关闭。严格路径禁止在线
fundamental/event enrichment 和外部 benchmark，也不会构造或调用 provider。

这是一条两阶段离线物化边界：QUANTAXIS adapter 可在受控 worker 中产生或验证带固定
`source_versions` 的数据结果，生产 runner 消费 canonical snapshot；runner 本身不直接加载
QUANTAXIS，也不把 worker 依赖加入主环境。旧 `legacy` 路径保持兼容，但只能把未验证复权记为
`provider_default_unverified`，不能宣称满足 QE2 snapshot 契约。

run card 现保存 snapshot/request hash、复权、频率、区间、逐证券实际 source、source version、单位和
availability context hash。旧 `source=auto` 路径在首选空结果后由 fallback 成功时，也记录真正返回
数据的 source，不再用路由推断值代替事实。

严格生产重放覆盖 DT-05：修改请求区间之后的 provider 状态不会触发联网，也不改变既有
data provenance、metrics 或 equity artifact；只有被选择的 snapshot 内容修订才改变 identity。
AKShare strict capability 最终只 adopt 股票 raw；ETF/指数因没有经审查的 raw OHLCVA/单位契约而
drop。Tencent、Mootdx、Eastmoney、BaoStock 和 local 同样从 QE2 strict path drop；旧 registry
兼容性不受影响。

第五切片定向测试为 `7 passed in 2.61s`；QE2/QE0/QE1 与生产 runner/run-card 组合门禁为
`284 passed, 7 skipped in 14.39s`；脱敏占位 Tushare 环境完整后端为
`5448 passed, 12 skipped, 20 warnings in 180.19s`。QE2 五个切片和 G1 至此关闭；生产部署仍未切换。
