# 许可、来源与引用

本文说明 **软件许可** 与 **上游数据条款** 的边界。开源本仓库不等于可自由再分发采集到的行情或公告数据。

## 软件许可

- 本仓库源代码以 [Apache License 2.0](../../LICENSE) 发布（见 [NOTICE](../../NOTICE)）。
- 你可以对代码进行使用、修改、再分发（按 Apache-2.0 文本履行归属、变更声明与 NOTICE 保留等义务）。

## 数据不是随仓库附带的

- Git 仓库 **不包含** 生产数据湖（`data/`）、本地配置（`configs/cnequity.toml`）或运行日志。
- 运行 `cne init` / `cne run daily` 后，数据落在你本机（或你指定的 `data.root`）。这些内容的版权与使用限制由 **各数据源提供方** 决定，而非本项目的 Apache-2.0 许可。

## 上游来源（摘要）

本引擎通过适配器访问多家公开/半公开接口与文件，包括但不限于：

| 来源 | 典型用途 | 备注 |
|------|----------|------|
| 通达信协议（内置客户端） | 日线、指数、除权、证券列表、分钟线、分笔 | 需可达的行情服务器 |
| 东方财富相关 HTTP 接口 | 资金、估值快照、公司行为、结构等 | 非官方 SDK；限速与字段可能变更 |
| 新浪等行情 HTTP | 部分备源 / 快照 | 同上 |
| Baostock | 估值与 ST 历史回填等 | 遵循其用户协议与访问频率 |
| 巨潮资讯 | 监管/公告类 | 遵循站点使用条款 |
| 中国人民银行 调查统计司 | 社会融资规模增量 | 官方统计表附件（xls/xlsx），仅取月度增量列 |
| 申万研究公开分类文件 | 行业分类历史 | 公开 XLS；解析依赖随基础安装提供 |
| 上交所 / 深交所公开发布 | 交易日历备源、ST 简称核对、`daily_bars` 权威比对、`margin_trading` 主源 | 交易所自行编制并公开发布，权威性高于任何转发方；发布页条款仍需逐接口确认 |

优先用发布方而不是转发方（见 [产品边界](architecture/overview.md)）：能直接读到编制该数据的机构时，
就优先核对原始发布证据。来源权威性与使用许可分别判断：交易所公开发布也不能推定允许缓存、商用或再分发。仓库已登记的限制与待审状态见[来源矩阵](legal-and-data-sources.md#来源合规矩阵)，本次文档整理不代表重新审阅上游条款。

逐数据集主源、备源与限制见 [逐源限制](datasets/sources.md) 与 [数据集目录](datasets/catalog.md)。

### 关于「分笔」（`trade_ticks`）的口径声明

这一条单列，因为它最容易被误读成本项目并不提供的东西：

- 通达信分笔来自 **Level-1 行情的 3 秒快照**，是聚合结果。每条可能聚合多笔真实成交。
- 它**不是**交易所逐笔成交，**更不是**逐笔委托或十档盘口。
- 时间戳精度为**分钟**（秒位恒为 `00`），不是交易所时间戳。
- `direction`（买/卖/中性）是通达信按 tick rule **推断**的方向，与交易所口径不保证一致。
- 完整逐笔 / Level-2 需向交易所或授权服务商购买。本项目**不提供、不代理、不绕过**任何此类授权。

本仓库的文档与代码注释一律按上述口径表述；如果你在下游把它当作 Level-2 使用，风险与合规责任由你自负。

## 你的责任

使用本软件即表示你理解并同意：

1. **合规自负**：遵守所在司法辖区法律，以及各上游网站/API/SDK 的服务条款、版权与访问限制（含爬虫与商业使用限制）。
2. **不提供数据再分发授权**：维护者 **不** 授予你再分发、转售或公开托管 curated Parquet 的权利；是否允许取决于上游，而非本仓库。
3. **无可用性担保**：上游改版、封禁 IP、证书或限速导致失败时，引擎应暴露失败而不是塞假数；这不构成对本项目的缺陷索赔依据（除非代码本身违反已文档化的数据契约）。
4. **密钥与出口**：代理、Cookie、本机路径等属于你的运行环境；请勿提交到 git 或 issue 附件。

上游条款与合规边界见上文；实现细节见 [逐源限制](datasets/sources.md)。

## 安全问题

漏洞请按 [SECURITY.md](../../SECURITY.md) 私下报告，不要在公开 issue 中粘贴凭证或完整本地配置。

## 与定位文档的关系

若你在评估「是否该用本项目还是 akshare / Tushare」：先读 [是否适合我](architecture/overview.md#是否适合我)，再读本文确认数据合规边界。

## 来源合规矩阵
`sources/SOURCES.yml` 是数据集注册表之外的来源合规登记。它只登记来源标签、访问方式、条款审阅状态和保守的使用结论，不替代任何上游服务协议，也不向下游授予数据使用或再分发许可。

### 覆盖范围

矩阵的来源集合来自 `src/cnequity/domain/datasets.py` 中每个 `DatasetSpec` 的 `primary_source`、`backup_source` 和 `backfill_source`。因此备源和只用于历史回填的来源也必须登记。`derived` 是一个特殊的显式来源标签：它表示本地派生结果，不能把它当作独立的数据许可来源。

仓库当前登记 15 个来源标签；下面的历史审阅说明只解释登记背景，不代表当下所有端点或条款已重新验证。可以用下面的只读检查确认注册表与矩阵仍然一致：

```python
from cnequity.compliance.source_policy import load_source_policies, required_sources

policies = load_source_policies()
assert required_sources() <= policies.keys()
```

截至 2026-08-29，矩阵已经记录东方财富和同花顺的官方用户许可页面及限制性结论；其余来源仍保持待核实状态。

2026-09-13 新增 `ths_official`（同花顺官方 API，fuyao.aicubes.cn）。它与 `ths` 是两个不同的来源标签：`ths` 抓取 10jqka 公开页面且并非已登记客户端，`ths_official` 是账号签发 API Key 的已登记客户端，因此 `authentication` 记为 `api_key`。但站内文档（含 llms-full.txt 全文聚合）没有任何关于数据商用、再分发、缓存或留存的条款，只有一句「数据权限以官网与账号授权为准」，所以 `commercial_use`、`redistribution`、`cache_allowed` 全部保持 `unknown`，`cne sources policy ths_official` 因此返回 `review_required`。上游仓库的 MIT 许可只覆盖代码，不涉及数据。

同日补登 `bse`（北京证券交易所）。该来源用于 BJ 名单、状态和当期行情；`bse_*` 子标签沿用其登记政策。
北交所站点在境外出口返回 403，条款页不可达，因此权限字段全部保持 `unknown`。
子标签 `bse_*` 按前缀继承本条政策，无需改动适配器。这里的“已审阅”只表示维护者把页面中的明确限制转换为保守的机器状态，不等于律师出具的完整法律意见。

### 字段与保守语义

每个来源至少包含 `owner`、`access_type`、`tos_url`、`tos_reviewed_at`、`authentication`、`personal_use`、`commercial_use`、`cache_allowed`、`redistribution`、`rate_limit`、`retained_payloads`、`legal_status` 和 `notes`。未核实的事实必须填写精确的 `unknown`，不能用空值、猜测的日期或含糊的“公开所以允许”替代。

`personal_use`、`commercial_use`、`cache_allowed` 和 `redistribution` 只有明确的 `allowed`（或布尔 `true`）才会被使用策略视为允许；`unknown` 一律产生待审阅/阻断结果。`tos_reviewed_at` 只有在对应条款确实被人工审阅后才可写日期；代码仓库的 Apache-2.0 许可证不改变上游数据的限制。

`policies_for_dataset("daily_bars")` 可按主源、备源和回填源汇总政策；`usage_profile(...)` 可对个人使用、商业使用、缓存或再分发意图作保守风险判断。该 API 只提供机器可读的风险门槛，不构成法律意见。

### 生成与维护流程

矩阵不是运行时从网络抓取的清单。新增或修改 `DatasetSpec` 时，维护者应：

1. 重新计算注册表中的唯一来源标签，并为每个新标签补齐矩阵必填字段；`derived` 只能在确实为本地派生时标记为 `true`。
2. 针对来源方的当前官方条款、接口说明、许可文本和限速/缓存规则逐项核实。无法核实的项保持 `unknown`；不要根据接口无需登录、网页可访问或其他来源的许可推断允许商业使用或再分发。
3. 在同一变更中更新 `tos_url`、`tos_reviewed_at` 和 `notes`，说明审阅范围与日期；复合标签（如 `eastmoney_kline+sina_global`）必须分别审阅所有组成来源。
4. 运行来源政策单元测试与 `ruff check`。若政策文件改用外部路径，调用方应在使用前显式运行 `validate_source_policies`，不要绕过校验。
5. 发生条款变更、来源迁移、服务停用或权限撤回时，回退为 `unknown` 或写入明确限制，并保留变更说明；不要把历史上的“曾经可访问”当作当前授权。

本矩阵只描述仓库实现所观察到的访问方式和待确认事项。使用者仍须自行阅读上游条款、评估所在司法辖区要求，并对采集、保存、商业使用及再分发承担责任。

## 引用项目
如果 cnequity 帮助了你的论文、研究报告或数据工程，请引用仓库。GitHub 会读取根目录的 [`CITATION.cff`](https://github.com/rootSunc/CNEquity/blob/main/CITATION.cff)，并在仓库首页提供 “Cite this repository” 入口。

### 软件引用

```text
CNEquity Contributors. (2026). CNEquity: A free, self-hosted historical
financial data infrastructure for China markets, starting with A-shares
(Version <your installed version or commit>). Apache-2.0.
https://github.com/rootSunc/CNEquity
```

版本化研究请同时记录：

- `cnequity` 版本或 Git commit
- 数据湖的 `coverage_start` / `coverage_end`、依赖 revision 映射与研究快照身份
- `adjust`、`strict_adj`、profile / `scope_hash`、`strict_universe`、`as_of` 与 `pit_mode` 口径
- 上游数据源及其许可限制

软件许可证是 Apache-2.0；落盘行情、公告和财报仍受上游条款约束，不能仅凭软件引用获得再分发权。见本页「软件许可」与「你的责任」。
