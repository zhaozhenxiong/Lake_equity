# 仓库脚本

路径：`scripts/`

| 目录 | 内容 |
|---|---|
| `scripts/scheduler/` | 调度：日更 / 事件流 / 补抓管线、健康通知、元数据备份、launchd 模板与安装（改动路径后重跑 `install_scheduler.sh`，已安装任务引用的是绝对路径） |
| `scripts/` | 运维工具：回填验收、退市名录重建、补跑追赶、幸存者偏差测量 |
| `scripts/migrations/` | 一次性数据迁移与分区重写，按版本说明执行 |
| `scripts/dev/` | 仓库维护：文档同步（`sync_*`）、发布检查、离线基准与端到端检查、探针 |

这些脚本需要源码 checkout，不随 PyPI 包安装。仅用安装包时，每天调度一次 `cne run daily`（含全部日更组与事件流）；仓库日更以 `daily_pipeline.sh` 为主入口。

> 以下脚本面向 **自托管本机/VPS**（含 macOS launchd 与服务器回填）。开源贡献者只需
> CLI（`cne init` / `run`）即可；调度与告警按需选用，并非唯一部署方式。

## 生产脚本

### daily_pipeline.sh

**用途**：交易日串行执行全部 schedule groups，末尾健康检查与备份。

**流程**：

```
for group in $(python -m cnequity.orchestrator.schedule_groups --config "$CNE_CONFIG"); do
  cne run daily --group $group
done
health_notify.sh
cne sources probe --stale-only --vantage $CNE_SOURCE_VANTAGE
cne sources slo             # 累积报告，日更不 enforce
cne verify --runs --days 20 # 累积报告，日更不 enforce
backup_meta.sh
cne run clean
```

较晚窗口由独立的 `stale_pipeline.sh` 只补仍落后的 snapshot 数据集，与主日更共用调度锁。这把锁在 `{data.root}/locks`，和 `cne serve` 操作页是同一把；`CNE_SCHEDULER_LOCK_DIR` 或 `CNE_LOCK_DIR` 可改目录。锁记录主机、PID 和进程启动身份，自动回收已确认退出的本机进程；只有 PID 的旧格式锁在该进程退出后也会回收，没有标记的锁目录超过 60 秒后回收。远程主机的锁须由维护者核验后移除。主脚本保留旧的进程内等待开关供兼容，但默认关闭。详见 [runbook · 收尾补抓](runbook.md#收尾补抓)。

**环境变量**（`CNE_CONFIG` / `CNE_LOG_DIR` 同时被 `cne` 本身读取，其余仅本脚本读取）：`CNE_CONFIG`, `CNE_LOG_DIR`（长跑命令自己也往这里写一份日志）, `CNE_GROUPS`,
`CNE_GATE_GROUPS`（默认 `core`，失败标为 gate；其余组标 soft）、
`CNE_SOFT_FAIL_OK`（默认 `1`：gate OK 时 soft 失败 exit 0；`0`=仍 exit 1）、
`CNE_STALE_RETRY`（默认 `0`；`1` 开启旧兼容补抓）、
`CNE_STALE_RETRY_DELAY_SEC`（默认 `1800`）、
`CNE_SOURCE_HEALTH`（默认 `1`；`0` 关闭每日过期证据探测）、
`CNE_SOURCE_VANTAGE`（默认 `local`；应设为稳定且真实的出口标签，如 `cn` 或
`overseas`）、
`CNE_TRADE_DATE`、
`CNE_BIN`（覆盖 `cne` 路径；供用 stub 跑通控制流的测试用）、
`CNE_PYTHON`（门控与调度锁使用的解释器；默认仓库 `.venv/bin/python`，没有 `.venv` 时用 PATH 上的 `python3`，须能导入 cnequity）。

结束时打印分组摘要（`group: OK|FAILED [gate|soft]`）外加 `stale-retry: OK|FAILED|not needed|skipped`，便于区分「门禁挂了」与「东财挂了」。补抓失败算 soft。

### events_pipeline.sh

**用途**：7x24 事件流（公告、监管事件、资讯）。`cne run events` 一次跑完
`[job.events.groups]` 里的所有组，或用 `CNE_EVENTS_GROUP` 指定一个。

**和 `daily_pipeline.sh` 分开的原因：**

- 仓库日更脚本按组调用 `cne run daily --group`，这一条不包含事件流。不带参数的 `cne run daily` 会在调度组之后自己跑事件流；非交易日跳过调度组，事件流仍然运行。
- 事件流使用单独的 shell 锁（`events`）和 `events_ingestion` 锁，可以和按组日更重叠。两边写入的数据集不相交，由配置校验保证。
- 公告和资讯在周末、节假日也会发布，所以这个任务按自然日调度，不看交易日历。

**环境变量**：`CNE_CONFIG`、`CNE_LOG_DIR`、`CNE_BIN`、`CNE_TRADE_DATE`、
`CNE_EVENTS_GROUP`（默认全部组）、`CNE_SCHEDULER_LOCK_DIR`。

上一次还在跑时直接跳过并 exit 0：每组下次都会重读自己的窗口，跳过一次不丢数据。

### stale_pipeline.sh

**用途**：晚一档的 stale-only 补抓，`cne run daily --stale-only`。

**为什么是独立任务**：正常的六组日更即使某个源挂了也必须**及时跑完并上报**；把 30 分钟的等待塞进那个进程里，会让一次源故障看起来像调度器超时了几小时。晚一点的第二个窗口照样能补上 snapshot-only 数据集，又不拖住正常那次。

**与日更互斥**：和 `daily_pipeline.sh` 共用 `scheduler_lock.sh` 的同一把锁。launchd 可能在主 pipeline 还没跑完时触发它——这时它**直接跳过**（退出 0）而不是排队，因为下一个窗口还能再试，而重复的 run 绝不该堆在采集后面。

环境变量：`CNE_BIN` / `CNE_CONFIG` / `CNE_LOG_DIR`，可选 `CNE_STALE_GROUPS`、`CNE_TRADE_DATE`、`CNE_PYTHON`（同上）。

### scheduler_lock.sh

日更、事件流、stale 三个 shell 任务共用的 launchd/cron 级互斥锁。

macOS 不带 `flock`，而 Python 那把 run lock 的作用域只有一次 `cne` 调用——日更脚本要跑好几条命令，所以需要一把覆盖整个脚本的锁。被 `daily_pipeline.sh` 与 `stale_pipeline.sh` source 进去用。

### scheduler_config.py

渲染并比对 launchd 任务，**保留本机已有的调度选择**。`install_scheduler.sh` 用它从一份配置生成三个 plist，而不是各自手写。

### install_scheduler.sh / uninstall_scheduler.sh

从 `scripts/scheduler/launchd/com.cnequity.daily.plist.template` 生成用户 launchd plist，加载 `daily_pipeline.sh`。
同时安装 `com.cnequity.stale`（收尾补抓）与 `com.cnequity.events`（事件流，每个自然日 14:00，按本机时区，不是北京时间；可按自己的披露节奏改）。
安装时用 `CNE_SOURCE_VANTAGE=cn scripts/scheduler/install_scheduler.sh` 固化真实出口标签；省略时
使用 `local`。标签仅允许字母、数字、点、下划线和连字符，防止生成无效 plist。

### health_notify.sh

```bash
# 平日
cne audit                 # 只看本次 run 的活跃分区
cne status --datasets --gate --groups "$CNE_GROUPS"
# 每周一次（默认周六）
cne audit --full          # 整湖结构扫描 + 刷新 health-latest.json
```

失败时 macOS `osascript` 通知，退出码非零。通知标题按失败的那一项分：
审计/健康那一支是「数据异常」，纯新鲜度落后是「数据滞后」。

**为什么分开**：`cne audit --full` 会扫描整个湖，I/O 成本随文件数量增长。日常优先按 run 审计，每周再检查整湖；实际耗时应记录在自己的运维日志中。

**环境变量**：`CNE_FULL_AUDIT_DOW`（默认 `6` = 周六，`1-7` 对应周一到周日；
`0` 完全关闭整湖审计；`always` 恢复每天都跑的旧行为）。

**为什么新鲜度门禁要按组收窄**：`CNE_GROUPS` 会原样传给 `cne status --datasets --gate --groups`，
也就是这台主机真正在跑的那些组。不收窄的话，只调度 `core` 的主机上有二十多个数据集
**没有任何任务去抓**，会持续显示 STALE，掩盖真正的异常。
门禁与调度共用同一个变量，两者就不会各说各话。无人调度的数据集仍然判失败：
"不知道谁抓"和"别的主机在抓"不是同一件事。

> 整湖审计里的分区碎片化 / 粒度混用检查只在 repartition 之后才会变化，
> 所以它们也一并移到了每周那次。手动 `scripts/migrations/repartition.py` 之后，
> 请直接跑一次 `cne audit --full` 复核，不要等到周六。

### backup_meta.sh

打包 `meta/manifest.db`、`state/`、`quality/`、revision **收据**、
`source_snapshots/`、`source_health/` 和 `stability/` 为
`meta-YYYYMMDD-HHMMSS.tar.gz`。不含 `meta/revisions/data/` 中的数据版本文件或
`curated/`；它是元数据备份，不能单独恢复完整数据湖。

参数：`backup_meta.sh [DATA_ROOT] [BACKUP_DIR] [RETENTION_DAYS] [RETENTION_COUNT]`。
不传 `DATA_ROOT` 时使用 `CNE_DATA_ROOT`，否则使用仓库的 `data/cnequity`，
**不会**从 `CNE_CONFIG` 读取湖路径。默认保留 14 天且最多 30 份；
备份目录默认在湖内，需要磁盘级容灾时设置湖外的 `CNE_BACKUP_DIR`。
运行环境宜安装 `sqlite3` 命令，以便脚本用 SQLite 备份 API 复制运行中的 manifest；
缺少它时脚本退回文件复制，应在采集停止后备份。

### run_catchup.py

**用途**：漏跑 / 周末之后把门禁补齐——`daily:core`，再 `market_breadth` + `compact`
（`--core-only` 时跳过后者）。不传 `--backfill`（全量 CA 扫描在海外出口上很脆）。

```bash
python scripts/run_catchup.py                          # 最近一个交易日
python scripts/run_catchup.py --trade-date 2026-07-17
python scripts/run_catchup.py --trade-date 2026-07-17 --all-groups
```

**退出码看门禁，不看附加组**：core 或 market_breadth 失败 exit 1；`--extra-group` /
`--all-groups` 里的组失败只报告不改退出码——东财重的组在海外出口上本来就时好时坏。
水位已经到目标日的部分直接 `skipped_already_fresh`，不重跑。

原为 `cne run catchup`。它是编排而非能力（每一步都是 `cne run daily` 打一个 schedule
group），和 `daily_pipeline.sh` 是同一类东西，所以放在这里而不是发布的 CLI 里。

## Init 与验收

历史初始化与续跑直接使用公开 CLI：

```bash
cne init --profile full --since 2016-01-01   # 中断后重跑同一条命令即续跑
```

源能力诊断先用 `cne sources probe --list` 选最小探测；回填前先看
`cne backfill <dataset> --plan` 的范围与请求估算。

### accept_backfill.py

回填验收工具：

```bash
python scripts/accept_backfill.py snapshot --out /tmp/counts.json
python scripts/accept_backfill.py check --compare /tmp/counts.json
```

检查幂等性与 curated 行数稳定性。

### delisted_ops.py

重建退市宇宙（幸存者偏差修复）的四个子命令。**读**目录和拉行情留在 CLI
（`cne delisted status` / `cne backfill daily_bars --profile delisted`）——那两个有日常形态；这四个没有。

| 子命令 | 说明 |
|--------|------|
| `discover [--limit N]` | 扫 issued code space，分类为曾上市 / 从未发行；可续跑，探测失败的码保持 pending 而不会被记成「从未发行」 |
| `reconcile [--apply]` | 默认只读核对目录终点；`--apply` 只修正被正式退市日、交易日历和正成交量行情共同证伪的终点，并生成备份及 SHA-256 回执 |
| `repair [--since]` | **不重新拉行情**：用已有 `daily_bars` 跨度写 `instruments.delist_date`，并清掉 `认购款` 占位 |
| `coverage [--start] [--end]` | **只读严格门禁**：验证发现完整性、窗口重叠、末根有效成交和 instruments 身份；未通过退出 1 |

```bash
cne delisted status                                   # 已知多少
python scripts/delisted_ops.py discover --limit 500
cne backfill daily_bars --profile delisted --start 2016-01-01
python scripts/delisted_ops.py repair
python scripts/delisted_ops.py reconcile
python scripts/delisted_ops.py reconcile --apply
python scripts/delisted_ops.py coverage --start 2016-01-01 --universe all_a_sh_sz
```

只有 `coverage` 是门禁：未验证通过时退出 1，所以任何声称「幸存者安全」的流程都应该先过它。

`coverage` 的通过声明刻意很窄：它证明退市目录已扫完，且已知与窗口重叠的退市标的具备一致的
末根有效成交和证券主数据；它不证明两端之间每个交易日都连续。数据源在停牌或正式摘牌前可能保留
零成交占位行，门禁不会把它们误当成末次交易。目录末日晚于窗口、但窗口内又没有行情可证明已经
上市的标的默认进入 `unknown_overlap`。完整 Baostock 证券主档明确给出 IPO 晚于窗口时，列入 `not_yet_listed`；完整交易日历内每个应有交易日均有独立 Baostock 停牌证据时，列入 `verified_nontrading`。派生的行情缺口、缺少日期的日历或不完整停牌记录都不能作为通过依据；这两类均在报告中逐项保留。证券身份记录更新前保留内容寻址历史副本。

`reconcile --apply` 不以单一供应商返回的「最后一条记录」为真相：必须有 curated 正成交量终点，
且该终点不晚于 `instruments.delist_date`，同时旧目录日期还必须落在正式退市日之后或非交易日，
才允许自动修改。检测到任何 active ingestion run 都会拒绝执行；修改前的目录保存在
`meta/state/history/`，质量回执写入 `meta/quality/`。

原为 `cne delisted discover / reconcile / repair / coverage`。

## 一次性迁移

### repartition.py

把历史分区改写成 `DatasetSpec.partition_granularity` 配的周期
（见 [分区粒度](../architecture/lake-layout.md#日常管理)）。不带参数只列出待改写的数据集，
所以列表形式随时可跑。

```bash
python scripts/migrations/repartition.py                  # 待改写的数据集
python scripts/migrations/repartition.py --all --dry-run  # 先看影响
python scripts/migrations/repartition.py trading_calendar # 单个数据集
```

读路径按目录形状自解析，改粒度本身**不需要**迁移；这只是把碎文件收回来。写入是先建临时目录、
逐分区写完并核对总行数，再一次 rename 换上去，中途挂掉不动原数据；重复执行幂等。

原为 `cne repartition`。触发它的是 registry 粒度在既有湖之下变了——那是迁移，不是日常运维。

### migrate_daily_bars_volume_v2.py

把 curated `daily_bars.volume` 全部改写为「股」，并将 `data_version` 由 `v1` 提到 `v2`。

在修复之前，这一列混着两种单位：`tdx_protocol` / `sina` 写的是手，`ths` / `baostock`
写的是股，正好差 100 倍。存量行在任何一种口径下都是错的，只能重写。单位契约与迁移要求见
[Schema 契约 · 成交量单位](../datasets/schema.md)。

```bash
scripts/migrations/migrate_daily_bars_volume_v2.py --config configs/cnequity.toml --dry-run
scripts/migrations/migrate_daily_bars_volume_v2.py --config configs/cnequity.toml --apply
```

- `--dry-run`（默认）只统计不落盘；`--apply` **就地改写 curated**，先跑 `backup_meta.sh` 并备份 curated。
- 幂等：已是 `v2` 的行跳过，中断后可直接续跑。
- `fetched_at` 不重新打戳——记录这次重新解释的列是 `data_version`。
- 跑完用 `cne audit` 确认 `daily_bars_volume_unit` 无 finding。

## 测试与冒烟

### smoke_daily_e2e.py

端到端冒烟：mock 或轻量配置下跑 miniature daily 路径，CI/本地回归用。

## 文档同步与翻译

### dev/sync_docs.py

从代码重新生成文档里由代码决定的部分，中英文两套都包括：字段表、衍生品能力表、CLI 参数表、命令副作用清单，以及 PyPI 用的 README。`--check` 只检查不写入，CI 会跑。CLI 帮助的中英文来自包内翻译目录（英文写在代码里，中文在 `src/cnequity/locale/zh_CN/LC_MESSAGES/cnequity.po`），缺中文时 `python scripts/dev/i18n_catalog.py` 检查失败；副作用说明的英文在 `scripts/dev/i18n/cli_surface.en.yml`，新增命令而没有对应英文时，检查失败。

### dev/i18n_catalog.py

维护 CLI、终端进度和控制台服务端文案的翻译目录。文案在代码里写英文，用 `cnequity.i18n` 的 `_()`（运行时翻译）或 `N_()`（只登记，显示时再翻译，如 Click 帮助和日志模板）标记；命令的 docstring 就是帮助，自动登记。中文在 `src/cnequity/locale/zh_CN/LC_MESSAGES/cnequity.po`，编译出的 `.mo` 一并提交，安装包不需要构建步骤：

```bash
python scripts/dev/i18n_catalog.py update    # 补新文案、删掉代码里已没有的
python scripts/dev/i18n_catalog.py compile   # 由 .po 重新生成 .mo
python scripts/dev/i18n_catalog.py           # 检查；有问题退出 1
```

检查覆盖：标记过的文案缺条目或缺中文、条目已不在代码里、模糊条目、占位符与英文不一致、首尾换行不一致、`.mo` 与 `.po` 不符。`tests/unit/test_i18n_catalog.py` 会跑它；`tests/unit/test_cli_untranslated.py` 另外拦下没走目录的中文字面量。Babel 只是开发依赖，安装后用标准库读取 `.mo`。

### dev/doc_translations.py

列出落后于中文原文的英文页。每个英文页在 `scripts/dev/i18n/doc_sources.json` 记着所依据中文页的摘要；中文改了、英文没跟上，`tests/unit/test_docs_i18n.py` 就会失败。改完英文后登记：

```bash
python scripts/dev/doc_translations.py --mark reference/mcp.md
```

代码生成的表格不计入摘要，重新生成表格不会要求重译。

## launchd 模板

`scripts/scheduler/launchd/com.cnequity.daily.plist.template`

- `ProgramArguments` 指向 `daily_pipeline.sh`
- `StartCalendarInterval`：Minute=7（每小时唤醒）；`CNE_SCHEDULED=1` 让 `daily_pipeline.sh` 先问 `scheduler_gate.py`，每个交易日北京时间 `[job.daily] run_at` 之后只跑一次
- 标准输出/错误重定向到 `{data.root}/logs/launchd.*.log`

`scripts/scheduler/launchd/com.cnequity.stale.plist.template`

- `ProgramArguments` 指向 `stale_pipeline.sh`
- `StartCalendarInterval`：Minute=37（每小时唤醒）；当天日更跑过、且过了北京时间 `[job.stale] run_at` 才跑一次，只补快照类（`--stale-only --snapshots-only`）
- 与日更共用 `scheduler_lock.sh` 的锁；主 pipeline 还在跑时直接跳过

`scripts/scheduler/launchd/com.cnequity.events.plist.template`

- `ProgramArguments` 指向 `events_pipeline.sh`
- `StartCalendarInterval`：Hour=14, Minute=0，**不带 `Weekday`**——事件流本来就要在
  周末和节假日跑（UTC+2/+3 机器约合 20:00 CST）
- 想要资讯的日内新鲜度就再加一个 `CNE_EVENTS_GROUP=news_wire` 的高频 agent/cron；
  `disclosures` 每次重读 30 天尾窗，不适合高频

## 相关文档

- [运维 Runbook](runbook.md)
- [快速开始](../getting-started/quickstart.md)
