# 旧公开入口退役与能力迁移

本文件描述 2026-10-23 的统一入口迁移。旧运行记录、模型、行情、账户和不可变来源包保留；退役不重写历史身份，不授予自动纸上资格。

## 公开路径

`/api/khquant`、`/api/lake`、`/native` 及其全部子路径返回 404，包括旧 OpenAPI、静态页面和写请求。认证开关不会使这些入口重新可用。应用不再创建两个旧 FastAPI 应用，不使用前缀网关，也不挂载旧控制台静态目录。

| 原能力 | 当前入口 | 实现边界 |
| --- | --- | --- |
| 行情目录、查询、质量、日更 | `/api/v1/catalog`、`/datasets/{name}/read`、`/quality`、`/jobs` | 发布代次、读取凭证和显式任务继续核验 |
| 数据集详情、分区、日期与来源 | `/api/v1/data/datasets/{name}` 及 `/partitions`、`/dates`、`/provenance`、`/provenance/series` | 纯 LakeView 读取；日期和覆盖粒度按真实分区展示 |
| 采集历史、质量明细与覆盖 | `/api/v1/data/runs`、`/runs/{id}`、`/quality/runs/{id}`、`/heatmap` | 只读 manifest 和已存统计，不触发后台统计重建 |
| 旧预测历史图 | `GET /api/v1/predictions/{reference}/chart` | 原展示纯函数；只读 CAS，核验原输入和真实对象 SHA，不重新推理 |
| 旧任务列表和详情 | `GET /api/v1/jobs`、`/jobs/{provider:id}` | 直接只读旧 journal；不实例化 TaskManager、TaskJournal 或 LightResearch；不自动恢复 |
| 历史账户和日更记录 | `GET /api/v1/accounts` | 原 enabled_accounts 和 last_daily；只读展示，不自动续算或提升资格 |
| 历史账户图与选项 | `GET /api/v1/accounts/{result_id}/chart`、`/accounts/chart-options` | 保存的 Broker/Ledger 成交及原截止日期；不重算缺失交易 |
| 历史及新研究报告 | `GET /api/v1/reports`、`/reports/{id}` | 合并历史、学习和模型工作流索引，按各自身份校验读取 |
| 统一模型和滚动系列 | `GET /api/v1/models`、`/models/{id}`、`/learning/series/{id}` | 新模型注册表，不用旧模型候选替换新版本 |
| 新训练、最终拟合、选股和回测 | `POST /api/v1/jobs` | factor_training、model_refit、scan、backtest；旧 training 类型在接单前拒绝 |
| 输入检查 | `POST /api/v1/learning/preflight` | 显式计算真实发布数据；页面 GET 不自动训练 |

旧任务如果仍记录 running/queued/cancelling，新历史视图另外保留 recorded_status，并标识 historical，不把历史记录当作恢复后的活动执行。旧 khquant/light 任务不能通过新取消接口改写；现有 CNEquity 操作使用受核验的原操作记录取消合同。

## 数据运维

数据页调用 `/api/v1/data` 下的明确能力。服务类直接复用经过校验的操作构造、白名单参数、预览 token、配置备份和系统调度合同；不代理旧 HTTP handlers，也不接受任意命令或配置路径。

数据行读取继续通过 `/api/v1/datasets/{name}/read` 的发布读取凭证，显式支持 adjust=hfq/qfq/total_return 和 pit_mode=strict/best_effort。PIT 默认 strict；显式 best_effort 只改变浏览筛选，不把回填记录升级成当时可见。目录读取可能留下上游发布状态读锁的空 `.lock` 文件，不写入行情、历史 journal 或统计结果。

- 操作：`GET /operations`、`POST /operations/preview`、`POST /operations/start`；`GET /operations/jobs`、`/{id}` 和显式取消。
- 设置：`GET /settings`、`POST /settings/preview`、`POST /settings/apply`。只写白名单键，必须匹配预览 token；保留原文件备份，保存本身不采集。
- 调度：`GET /schedules`、`POST /schedules/preview`、`POST /schedules/apply`。核验配置、原状态和真实 OS 定时器身份后执行。
- 备份：`GET /backups`；创建、核验和向独立目标恢复通过 snapshot 操作预览及确认。
- 存储：`GET /storage`、`POST /storage/reviews`、`GET /storage/jobs/{id}`、`POST /storage/confirm`。精确对象预览和维护确认后执行。

原 21 项操作目录全部有说明；19 项可使用新白名单执行：

| 分组 | 操作 ID |
| --- | --- |
| 备份 | snapshot.create、snapshot.verify、snapshot.restore |
| 更新与事件 | daily.full、daily.group、daily.stale、events.run |
| 恢复与整理 | run.retry、run.retry_failed_groups、run.compact |
| 补采与派生 | backfill.run、derive.run |
| 检查 | diag.doctor、check.status、check.verify、check.audit、check.sources_probe |
| 维护预览 | maint.stats_rebuild、config.upgrade_preview |

init.start 和 init.resume 明确标为不可用。已有湖不能通过上游初始化替换受控、独立核验的交易日历；`backfill.run` 对 trading_calendar 同样拒绝。它们不是藏在旧控制台的备用入口。新的项目安装与受控日历修订应遵守现有安装和数据维护流程。

## 调度身份与实际执行

新页面保留同一个 ScheduleService/SchedulerBackend 及湖元数据身份派生的 `com.cnequity.web.<key>` OS 任务，不在新 Web 应用再启动一个内存调度器。Windows 使用 Task Scheduler，其他平台由原 backend 选择 cron/launchd。已有 enabled、时间和任务状态不因应用创建而改变，也不凭空创建新计划。

显式 apply 会实际调用 OS backend，重新观测注册结果后才保存已启用状态；不是仅写一份 JSON。原独立 `cnequity.serve.ops.scheduled` 入口继续读取同一配置，使用 tick.lock、control.lock 和会话 claim 防止重复执行。原旧研究 Web 应用的自动调度启动已随该应用实例一同移除。OS 计划已独立存在时，退役公开 HTTP 路径不会删除该计划。

## 学习引用与存储保护

清理仅针对数据湖生命周期拥有的历史发布代次或 staging；它不把模型、原始行情、输入快照或账户账本当普通缓存删除。

GET 页面只核验全部保留读取 manifest 并合并已有登记保护，返回 protection_scope=retained_read_manifests_and_registered_holds、full_reference_inventory=required_at_review、deletion_approved=false。页面摘要不全量散列历史报告，也不授予删除资格；独立历史 JSON 中尚未登记的直接 revision 引用仍须在检查时完整核验。

POST 存储检查前，`LearningReferences` 完整枚举 `var/learning`、`var/market_views`、`var/research_inputs`、`var/research` 中的保留文本证据，并保留已有生命周期 reference_roots。它读取训练 run、模型 manifest、工作流报告和训练输入里的 read_ref/training_read_ref、revision_map；同时保护全部保留读取 manifest，包括输入检查产生的 read_ref。滚动系列所指模型和训练 run 的来源由这些保留 manifest 覆盖，不依赖可变的“当前模型”指针。

每个 read_ref 都通过保存 manifest 的身份检查，并把其全部 revision_map 转成 `revision/<dataset>/<revision_id>` 保护引用，合并到生命周期 registry，不释放既有 holds。引用损坏、丢失、符号链接或扫描期间发生变化时拒绝规划，不解释为空引用。冻结报告和来源包不被修改。

确认时先阻止新增 API 读取和后台研究任务，要求活动研究/数据任务退出并排空已有读取；随后重新核对预览时引用指纹。新增或修改训练/模型/输入检查/报告引用会使旧预览失效。原生命周期执行器继续校验指定对象、当前代次、已有保护、发布回执、目录身份、实际文件 SHA、湖写锁、外部维护确认和一次性 token。并发重复确认不能重新打开正在执行的维护门。

操作者仍须确认外部读取与调度已停止；不自动关闭其他进程或修改原计划。新页面提供精确对象和字节、对象确认及外部维护确认，不能用一次全选直接清除全部历史。

## 验证与实际范围

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_legacy_migration.py tests\test_api_integration.py tests\test_learning_api.py -q
```

该组合在本轮实现时通过 37 项测试。覆盖旧工厂被禁止实例化且旧路径全 404、统一模型/系列路由、旧训练拒绝、新模型工作流和最终拟合分派、只读任务/账户/预测图、CAS 篡改拒绝、设置预览与原文件备份、OS backend 实际 apply 调用、学习引用阻止删除、预览后新引用使确认失效、重复确认不能打开维护门、数据详情/来源/采集/覆盖只读，以及合成无引用代次的精确删除。页面摘要不全量核验、但检查仍因坏学习引用而拒绝的边界亦有测试。真实盘中采集、真实 OS 新建/改变计划及生产湖删除没有在这组测试中执行。

存储删除测试仅在 pytest 临时合成湖中执行；原研究资产不受影响。测试还验证删除未引用代次后，已引用代次和原输入 SQLite 字节不变。已有 Starlette TestClient 的 httpx 弃用警告仍保留。
