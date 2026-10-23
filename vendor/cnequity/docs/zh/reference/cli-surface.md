# 联网与写入清单

由 `scripts/dev/sync_docs.py` 从 Click 命令注册表核对。‘执行时’指运行命令主体；`--help` 不执行。详细源选择与限制见[取数策略](../operations/fetch-policy.md)。

| 命令 | 第三方取数 | 本地效果 |
|---|---|---|
| `cne audit` | 条件 | 读取湖、输出审计；启用外部对照时访问源 |
| `cne backfill` | 执行时 | 补历史并写暂存与发布数据；--plan 仅读配置和状态 |
| `cne check` | 无 | 读取状态、新鲜度、审计结果与统计；统计过期时重算，--full 重跑全湖审计 |
| `cne config` | 无 | create 写个人配置；upgrade 备份后补调度 step；validate/diff 仅读 |
| `cne contract diff` | 无 | 读取契约并输出差异 |
| `cne contract show` | 无 | 读取契约；指定输出路径时写文件 |
| `cne contract validate` | 无 | 校验契约 |
| `cne decision-data cash-rights` | 无 | 读取湖并输出决策资料 |
| `cne decision-data payment-gaps` | 无 | 读取湖并输出决策资料 |
| `cne decision-data stock-terms` | 无 | 读取湖并输出决策资料 |
| `cne delisted status` | 无 | 读取退市状态 |
| `cne derive` | 条件 | 写派生数据；adj_factors 等模式可访问源 |
| `cne doctor` | 无 | 离线检查配置与环境 |
| `cne init` | 条件 | sample 离线；demo/quick/full 取数；layout-only 创建目录 |
| `cne mcp` | 条件 | 默认读湖；--live 可取源数据；--http 监听本地端口 |
| `cne profile list` | 无 | 列出内置范围 |
| `cne profile show` | 无 | 显示内置范围 |
| `cne query` | 条件 | SQL 读湖；按需数据缓存未命中或刷新时取数 |
| `cne repair adj-factors` | 仅 --refresh-sources 联网 | 默认离线预览并保存计划；--apply 发布有证据支持的修正、保留旧版本和未解决提示，分批保存历史进度 |
| `cne repair corporate-action-gaps` | --apply 时 | 默认离线预览；--apply 按缺口查询 Baostock 与巨潮资讯，核验后发布新版本并保留旧版本 |
| `cne repair daily-bar-coverage` | 无 | 默认只读预览；--apply 按已提交数据结清北交所整日欠账及已证明的上市前欠账并刷新覆盖水位，保存证据收据，不改日线 |
| `cne repair financial-announcements` | 无 | 默认只读预览；--apply 追加有 LICO 原始披露日证据的财报重建版本，经修复门禁发布并保留已有版本 |
| `cne repair financial-originals` | 仅 --refresh-sources 联网 | 默认离线回放原始定期报告；--apply 追加能明确识别的合并报表当期数，经修复门禁发布并保留已有版本和真实采集时间 |
| `cne repair fund-factors` | 无 | 默认只读预览；--apply 从原始来源序列补入已记录基金拆分和转增，经修复门禁发布并保留旧版本 |
| `cne repair fund-listing-dates` | 请求交易所当前 ETF/LOF 目录 | 默认预览；--apply 归档来源响应并补未知上市日期，经修复门禁发布并保留已有日期和旧版本 |
| `cne repair fund-no-print` | 仅 --refresh-sources 联网 | 默认离线预览；联网采证会归档报表并更新缓存；--apply 保存基金竞价日线覆盖证明、结清对应欠账并刷新水位，不生成日线或停牌行 |
| `cne repair fund-volume` | 仅 --refresh-sources 联网 | 默认离线预览；联网归档上交所行情服务的历史 K 线，金额冲突时复用或采集东财证据；--apply 核验后修复沪深场内基金成交量，经修复门禁发布并保留旧版 |
| `cne repair index-history` | 仅 --refresh-sources 联网 | 默认离线回放所选深证成指或科创50的已核验官方附件目录；--apply 按生效日替换完整历史名单，保留官方实际日期权重及旧版本；未知历史权重为空 |
| `cne repair index-snapshots` | 仅 --refresh-sources 联网 | 默认离线回放官方文件；--apply 按实际日期替换完整指数成分快照及真实权重，经修复门禁发布并保留旧版本 |
| `cne repair layout` | 无 | 默认只预览；--apply 发布新版本，保留旧版本和重复观察证据 |
| `cne repair misdated-valuation` | --apply 时 | 默认离线预览；--apply 向东财 datacenter 取被替换交易日的估值，核验后发布新版本并保留旧版本 |
| `cne repair missing-values` | 无 | 默认只读预览；--apply 将估值占位零及东财、国证缺失权重改为空值，经修复门禁发布并保留旧版本 |
| `cne repair orphan-symbols` | 无 | 默认预览，可能登记旧湖基线版本；--apply 移除无日线证券的因子和公司行为行，发布新版本并保留旧版本 |
| `cne repair stale-suspensions` | 无 | 默认预览，可能登记旧湖基线版本；--apply 移除成交日线否定的推断停牌，发布新版本并保留旧版本 |
| `cne repair status-listing-window` | 无 | 默认预览，可能登记旧湖基线版本；--apply 删除上市日之前和没有退市记录支撑的状态行，发布新版本并保留旧版本 |
| `cne repair status-outages` | 无 | 默认预览，可能登记旧湖基线版本；--apply 按现行规则重建窗口内的东财中断缓存行，发布新版本并保留旧版本 |
| `cne repair status-traded` | 无 | 默认只读预览；--apply 用已提交成交日线补缺失状态键，ST 保持未知，发布新版本并保留旧版本 |
| `cne repair stock-listing-dates` | 仅 --refresh-sources 联网 | 默认离线回放 IPO 日历；--apply 补官方上市事件证实的股票上市日期，经修复门禁发布并保留旧版本 |
| `cne repair valuation-basis` | 无 | 默认只预览；--apply 发布新版本并保留旧版本 |
| `cne run clean` | 无 | 仅预览过期文件；显式 --reconcile-runs 可修改运行状态 |
| `cne run compact` | 无 | 将已结束、未发布的暂存数据发布到湖 |
| `cne run daily` | 执行时 | 增量取数并写湖；默认含全部调度组与事件流 |
| `cne run events` | 执行时 | 事件取数并写湖 |
| `cne run retry` | 执行时 | 重试失败范围并写湖 |
| `cne serve` | 条件 | 启动湖面板。回环地址可从操作页发起白名单内的取数与日更；--read-only 关闭写入口；远程取数须 --allow-remote-ops；存储清理仍须网页确认；取数开关经预览确认后写回配置并备份，不因此启动取数 |
| `cne snapshot create` | 无 | 创建本地快照 |
| `cne snapshot delta apply` | 无 | 应用本地增量包；--dry-run 仅校验 |
| `cne snapshot delta create` | 无 | 创建本地增量包 |
| `cne snapshot delta verify` | 无 | 校验本地增量包 |
| `cne snapshot export` | 无 | 导出本地快照 |
| `cne snapshot import` | 无 | 导入本地快照 |
| `cne snapshot restore` | 无 | 恢复本地快照到目标目录 |
| `cne snapshot verify` | 无 | 校验本地快照 |
| `cne sources limits` | 无 | 读取出口预算、冷却及本地欠账；不探测源 |
| `cne sources policy` | 无 | 读取源政策 |
| `cne sources probe` | 条件 | --list 离线；探测时访问源并写报告 |
| `cne sources resilience` | 无 | 读取登记源与探测证据；可写报告 |
| `cne sources slo` | 无 | 读取探测历史并写统计 |
| `cne sources substitutes` | 条件 | 默认读报告；--probe 访问源 |
| `cne stats rebuild` | 无 | 重建本地统计 |
| `cne stats show` | 无 | 读取本地统计；过期或明细视图需要时自动重算 |
| `cne status` | 无 | 读取运行状态与数据集覆盖 |
| `cne storage apply` | 无 | 复核版本计划并 mark；CLI purge 禁用，删除须在 serve 网页确认 |
| `cne storage explain` | 无 | 读取版本保留依据 |
| `cne storage hold` | 无 | 增加对象保护并取消待删除标记 |
| `cne storage import` | 无 | 校验并登记引用清单；只增加保护 |
| `cne storage inspect` | 无 | 读取版本与保留原因 |
| `cne storage plan` | 无 | 校验保留依赖并保存版本计划；不删除数据 |
| `cne storage raw-external` | 无 | 登记外置原文目录；本地保留来源记录，不移动或删除文件 |
| `cne ths-official backfill` | 执行时 | 调用凭证源并补缺 |
| `cne ths-official capture` | 执行时 | 调用凭证源并写对照证据 |
| `cne ths-official repair-bars` | 执行时 | 预演也取数；--apply 写修复 |
| `cne ths-official resource-sectors` | 执行时 | 预演也取数；--apply 写暂存 |
| `cne verify` | 条件 | 默认本地校验；--repair 访问源并修复 |
