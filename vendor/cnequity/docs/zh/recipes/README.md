# 研究示例：按问题选择入口

先准备对应数据，再运行查询。基础 demo 只有小范围行情；不能直接用于财报、全市场股票池或衍生品研究。

| 想验证什么 | 示例 | 前提 |
|---|---|---|
| 未复权与后复权收益的区别 | [复权研究基线](research-baseline.md) | `demo --research`，需要 TDX 与 Sina |
| 调仓日可见的财报版本 | [PIT 财报截面](pit-rebalance.md) | 正式湖含财报与当时观察证据；新回填可能没有 strict 结果 |
| SQL 聚合与 Polars 特征处理 | [DuckDB 与 Polars](duckdb-polars.md) | 对应数据已 compact；复权例子另需因子 |
| 期限结构、期权链与 Greeks | [商品期货与期权](derivatives.md) | 显式启用、按交易所补数并验收 |
| 15 / 30 / 60 分钟线的现算与入湖 | [15 / 30 / 60 分钟线](minute-bars-15-30-60.md) | 已开启并回填 1m 或 5m |

## 所有示例共用的约定

1. 使用各页明确指定的配置。跨工作目录或 MCP 使用绝对配置路径与绝对 `data.root`。
2. 复权研究使用 `strict_adj=True`；PIT 使用显式 `pit_mode="strict"`；股票池选择版本化 profile 或 `strict_universe=True`。
3. `list_datasets()` 的覆盖边界是查找数据的起点，不证明窗口中间没有缺口。结合实际日期、质量报告和研究门禁判断。
4. 固定依赖 revision、查询参数、软件版本与必要证据。长期留存使用研究快照；示例不是回测收益或投资效果承诺。

API 细节见[查询指南](../datasets/query-guide.md)与[Python API](../reference/python-api.md)。
