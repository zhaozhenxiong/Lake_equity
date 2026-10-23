# 多因子学习与回测

新模型族 `cross_sectional_return_v1` 通过学习层读取已发布的 lake 数据代际，输出截面收益分数、逐窗口样本外指标和本地组合回测。原有 `ma_trend_v1` 及三分类权重保留历史身份，新任务统一使用收益回归模型。

网页入口为当前 Dashboard 的“模型中心”。统一训练、版本和实验视图；Kronos 使用独立页签。进入页面只读取能力目录和已保存报告，显式提交后才训练。任务页与内嵌进度使用同一取消接口。运行条件就绪不代表策略有效。

## 数据与学习边界

- 支持 1–6000 只显式沪深股票或 `scope=all`。全市场人口来自固定 instruments 代际，每批 200 只构造特征并写独立 Parquet，处理后再次核验来源实际 SHA。数值估计器的选定窗口仍进入内存，预检显示规模估算；这不是分布式训练。当前人口和显式样本可能存在事后选择偏差。
- 数据来自固定的 lake generations，不要求旧研究 SQLite 的 `input_manifest_id`，不进行全量 SQLite 复制或原地改写，不自动采集缺少的数据。
- 首期因子组为量价 `price_volume`、融资融券 `margin` 和可选资金流 `money_flow`；实际可用字段、滞后与缺失覆盖以本次报告为准。没有真实可得时间的财务、新闻与板块快照不自动获得严格历史资格。
- 每个滚动窗口独立拟合训练期处理器，训练、验证、测试标签边界按收益窗口隔离。验证集用于模型选择，最终比较基于逐窗口样本外预测。
- `local` 与 `qlib` 使用同一数据与时间切分合同，支持 Ridge / LightGBM / MLP。独立 Qlib 环境负责其数据组件和传统模型；MLP 复用主环境 Torch 并明确记录 `torch.MLP(shared_qlib_feature_contract)`、实际设备和版本，不冒充独立环境中不存在的 Torch。明确请求 CUDA 不可用时失败。
- Qlib 的 `StaticDataLoader`、`DataHandlerLP`、`DatasetH` 和模型接口用于学习数据接入；本地交易日历负责滚动切分，本地 Broker 负责执行约束。Parquet 接入不代表已实现完整 Qlib provider 或原样支持全部 `qrun` / `PortAnaRecord`。
- 量价因子参考 Alpha158 的设计；除非逐项验证 OHLC、VWAP、成交量单位、复权、算子与缺失语义，不称为精确 Alpha158 复现。

标签、成交时点、手续费、停牌与涨跌停处理的实际规则以报告及组合执行局限为准。首期为研究工具，任务完成或收益为正不授予严格历史有效性、未来盈利或模拟配置资格。

## 已验证运行环境

2026-10-23 已在 Windows Python 3.12 的独立 `var/runtimes/qlib` 环境中，通过 Qlib `StaticDataLoader`、`DataHandlerLP`、`DatasetH`、`LGBModel` 及 Ridge 的合成数据 fit/predict 冒烟验证。两种模型各产生 60 行有限预测，并保留 3 行尚未成熟标签的推理样本。验证记录位于 `var/runtimes/qlib/verified.json`；该检查验证组件可运行，不验证策略有效性。

本次实际安装 PyPI 提供的 `pyqlib==0.9.7` Windows Python 3.12 wheel，配套 `numpy==2.3.5`、`pandas==2.3.3`、`mlflow==2.22.5`、`lightgbm==4.7.0`、`scikit-learn==1.9.1`。GitHub v0.9.8 的源码参考不代表实际运行版本。完整实测 freeze 已保存为项目根目录 `requirements-qlib.lock`，不改动主环境 NumPy/Pandas。

在项目根目录复现环境并验证：

```powershell
py -3.12 -m venv var/runtimes/qlib
.\var\runtimes\qlib\Scripts\python.exe -m pip install -r requirements-qlib.lock
.\.venv\Scripts\python.exe scripts/verify_learning_runtime.py --backend qlib
```

最后一步通过后生成运行验证记录，能力目录才将 Qlib 后端显示为可用。

## API

`GET /api/v1/learning/catalog` 返回模型族、因子组、后端状态、默认参数和规模上限。

`GET /api/v1/learning/runs` 返回 `{ "runs": [...] }`。`GET /api/v1/learning/runs/{run_id}` 读取并校验保存报告；读取不触发训练。

通过 `POST /api/v1/jobs` 提交，得到任务标识后使用现有 `/api/v1/jobs/{id}`、`/result`、`/cancel` 接口：

```json
{
  "kind": "factor_training",
  "params": {
    "symbols": ["600519.SH", "000001.SZ", "600000.SH"],
    "start": "2024-01-01",
    "end": "2026-10-22",
    "factor_groups": ["price_volume", "margin"],
    "backend": "local",
    "models": ["ridge", "lightgbm"],
    "horizon": 5,
    "train_sessions": 252,
    "valid_sessions": 63,
    "test_sessions": 63,
    "max_folds": 3,
    "top_k": 2,
    "initial_cash": 1000000,
    "seed": 42
  }
}
```

以上仅为接口示例，不表示给定股票与日期满足实际覆盖要求。

## CLI

```powershell
.\.venv\Scripts\python.exe run.py learn --symbols 600519.SH,000001.SZ,600000.SH --start 2024-01-01 --end 2026-10-22 --groups price_volume,margin --backend local --train-sessions 252 --valid-sessions 63 --test-sessions 63 --max-folds 3 --top-k 2
```

可通过 `--models ridge lightgbm mlp`、`--device cuda`、`--max-epochs 50`、`--scope all`、`--preflight`、`--horizon`、`--initial-cash` 与 `--seed` 显式控制。CLI 同步输出进度及最终报告；训练前不会自动回填数据。

已安装项目包的环境也可用 `python -m lake_equity.cli` 替换 `python run.py`。使用已验证独立环境学习时将 `--backend local` 改为 `--backend qlib`。

## 产物与复核

报告保存于 `var/learning/runs/learn-*/report.json`；清单记录数据来源代际、特征列、窗口、模型配置和产物 SHA256。默认 38 项量价已经包含 20 项均线，加 5 项融资融券共 43 项，不重复拼成 63 项。同时选择三个算法时对比八组：均线 Ridge/MLP、纯量价 Ridge/LightGBM/MLP、多因子 Ridge/LightGBM/MLP。实际维度按每折训练有效列记录。任一请求的因子组在训练窗口全空都会阻断，不无声降级。

`report.benchmark` 保存同一样本的等权参考账户，也经真实 Broker 执行并扣除费用。`models[].excess_return_vs_equal_weight` 为模型与等权账户的总收益之差，页面以百分点展示，不是除以基准收益的相对增长率。它也不代表相对沪深 300 等市场指数的超额收益。

页面展示 IC、Rank IC、RMSE、样本量、研究账户收益（费用后）及相对等权基准的差异，并列出成交、拒单、部分成交、费用和沿用旧价估值日；所有比较应核对同一股票池、测试窗口、费用与执行规则。

Broker 支持沪深主板、科创板和创业板的有日期研究规则；科创板最少 200 股、其后按单股递增，创业板 2020-08-24 与主板 ST 2026-07-06 的限幅切换分别处理。完整规则、未知状态、拒单及严格证据模式见 [执行规则](EXECUTION_RULES.md)。期末库存保留，不强制清仓。复权、公司行为与真实执行证据仍不完整，研究账户收益不能称实盘收益；Sharpe 不足时显示缺失。

## 冻结模型、推理与最终重拟合

`run_id` 是实验，`model_id` 是不可变权重包，`series_id` 是按日期切换的样本外模型序列。注册包包含有序特征、训练期预处理、权重、read_ref、训练人口、日期、源码 SHA、设备与资格，使用前核对实际文件。Ridge 使用 JSON、LightGBM 使用原生文本、MLP 使用 `allow_pickle=False` 读取的 NPZ。

MLP 为 `d→64→32→1`，使用 GELU、Dropout、AdamW 和验证 MSE 早停。默认 CPU、50 epochs、patience 6、batch 2048、学习率 .001、weight decay .001、dropout .1。所有算法共用数据合同；填充、截尾、均值和标准差只拟合训练期。标签为 `close[T+h]/open[T+1]-1`，未来路径连续核验，未成熟标签保留为缺失并继续推理。

最终重拟合是独立显式 `model_refit` 任务：选择原实验与一个模型协议，继承固定参数，生成新的权重身份。它不覆盖原 OOS、不将旧收益套给新权重、不自动选择 champion 或纸上账户，也不生成虚构的滚动评价序列。

逻辑 `information_cutoff` 包含训练和验证标签真正使用的最后日期，实际 `frozen_at` 单独记录。今天构建的模型用于更早历史只能称研究重建，不能伪称当时已上线。软件升级、训练完成或收益为正不授予 paper 或前瞻资格。

## 统一选股和回测接口

全部公开接口使用 `/api/v1`。旧控制台及代理路径返回 404，所需能力迁移见 [退役映射](LEGACY_RETIREMENT.md)。

| 接口 | 用途 |
| --- | --- |
| `POST /learning/preflight` | 完整请求人口的真实数据覆盖、标签、窗口和设备检查 |
| `GET /models`、`GET /models/{id}` | 冻结模型、系列索引及内容校验 |
| `GET /learning/series/{id}` | 样本外模型切换合同 |
| `POST /jobs`，`kind=model_refit` | 参数为 `run_id`、`model_name` 和可选 `end` |
| `POST /jobs`，`kind=scan` | 参数为 `model_id`、`scope`、`symbols`、`end` |
| `POST /jobs`，`kind=backtest` | `mode=frozen` 传 `model_id`；`mode=rolling` 传 `series_id`，另传 `start`、`end`、`top_k`、`initial_cash` |

选股只加载权重一次，跨 200 股批次复用。特征计算保持训练时相同的历史起点与预热前缀，避免均线近乎相等时，不同浮点累计路径改变排列特征；不舍弃精度或给旧权重换公式。先固定完整人口，再统一排名；scored、missing、failed、unprocessed 分母完整保留。未完成任务不公布最终排名。实际行情日和请求截止日分别展示，网页对全部结果分页。新股票标记是否来自训练请求池；缺失因子使用原训练中位数并披露数量。

固定模型回测必须开始于全部训练和验证信息截止之后。滚动回测加载原冻结数据和对应折权重，只允许原 OOS 区间和评价人口。不能把今天 Top K 回填过去。两个入口共同使用 Top K、权重和真实 Broker/Ledger 共享现金账户。

```powershell
.\.venv\Scripts\python.exe run.py model-scan --model-id MODEL_ID --scope all --end 2026-10-22
.\.venv\Scripts\python.exe run.py model-backtest --mode rolling --series-id SERIES_ID --start OOS_START --end OOS_END
.\.venv\Scripts\python.exe run.py model-refit --run-id RUN_ID --model-name factor_mlp
```

不可变版本位于 `var/learning/models`，新选股和回测位于 `var/learning/workflows`，均使用 SQLite 索引和内容清单。失败构建保留隔离目录。融资融券 T+2 仍为披露的研究假设，当前人口、复权和公司行为仍未认证；负结果如实保存，不按测试收益自动调参或翻转信号。

上游参考：[StaticDataLoader](https://github.com/microsoft/qlib/blob/v0.9.8/qlib/data/dataset/loader.py)、[处理器](https://github.com/microsoft/qlib/blob/v0.9.8/qlib/data/dataset/processor.py)、[LightGBM](https://github.com/microsoft/qlib/blob/v0.9.8/qlib/contrib/model/gbdt.py)、[滚动重拟合示例](https://github.com/microsoft/qlib/blob/v0.9.8/examples/rolling_process_data/workflow.py)。这些链接用于设计依据；实际运行版本以环境与报告记录为准。
