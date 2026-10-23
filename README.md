# LakeEquity

本地 A 股数据湖与量化研究 Dashboard。将 CNEquity 的版本化行情、融资融券等因子接入统一训练，使用 Ridge、LightGBM 与均线多因子 MLP，训练后的冻结模型可用于全市场选股和组合回测。

LakeEquity 自有代码采用 [MIT](LICENSE)。内置 CNEquity、CZSC、Kronos 和前端图表资源保留各自许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 安装与启动

当前安装路径面向 **Windows x64、Python 3.12**。从本仓库克隆，或下载 Releases 中的源码 ZIP 并解压，在项目目录执行：

```powershell
py -3.12 install.py --device cpu
.\lake.bat serve
```

打开 <http://127.0.0.1:8130>。安装程序创建独立 `.venv`、本机 `configs/cnequity.toml` 和运行目录 `var/`，从依赖源联网安装软件，并核验随包原生引擎及公开源码 SHA-256。

NVIDIA 环境可显式选择 `--device cuda`，安装 CUDA 13.0 版 PyTorch；实际执行设备以任务报告为准。仓库包含上游 macOS ARM64 原生 wheel，但本次发行未执行 macOS/Linux 安装验收，不能据此声称跨平台可用。

**首次安装不附带行情、训练权重、Kronos 模型资产、个人账户或历史研究记录。** 请在数据湖中配置并采集你有权使用的数据，检查发布版本、交易日历和覆盖范围后再训练；缺少数据时页面保持空状态。Kronos 需另行准备合法模型资产。首次启动和能力目录就绪不代表数据已经完整。

## 研究流程

1. 在“数据湖”检查数据集、真实日期覆盖、质量和来源，显式执行所需采集任务。新数据必须发布为固定代际，快照数据不能冒充历史已知信息。
2. 在“模型中心”选择股票池、训练窗口、因子组和设备，先运行输入检查，再提交训练。量价 38 项已含 20 项均线特征，加 5 项融资融券后共 43 项，实际有效列以报告为准。
3. 查看各滚动窗口的 IC、Rank IC、误差、费用后模拟收益和同样本等权基准。处理器仅拟合训练段，训练、验证与测试标签按时间隔离。
4. 在“全市场扫描”明确选择一个冻结模型，对固定沪深 A 股人口分批推理并统一排名；缺失、失败和未处理股票保留在结果中。扫描不会重新训练。
5. 在“方法回测”选择固定模型或原滚动样本外模型序列。Broker/Ledger 共同处理共享现金、下一可执行开盘、整手、T+1、费用、拒单和期末库存。

MLP 使用 `d→64→32→1`，与传统模型共用有序因子和训练期预处理。最终重拟合会生成独立模型身份，不覆盖原样本外结果，也不自动设置 champion 或开启纸上账户。

Dashboard 同时提供结构与量价、板块特征、关注与账户、任务与报告。当前公开后端统一为 `/api/v1`；旧控制台及 `/native`、`/api/lake`、`/api/khquant` 已退役。冻结第三方源码继续作为内部计算库使用。

## 可选 Qlib 环境

默认本地后端可运行三类算法。Qlib 后端使用独立环境，避免改变主环境数值依赖；`requirements-qlib.lock` 是 Windows Python 3.12 的固定依赖清单：

```powershell
py -3.12 -m venv var/runtimes/qlib
.\var\runtimes\qlib\Scripts\python.exe -m pip install -r requirements-qlib.lock
.\.venv\Scripts\python.exe scripts/verify_learning_runtime.py --backend qlib
```

验证通过后才将 Qlib 后端显示为可用。其数据组件和传统模型接口接入本地固定代际；MLP 使用主环境 Torch，并如实记录实际引擎。量价特征参考 Alpha158 设计，尚未声称逐项精确复现 Alpha158。

## 核验与文档

```powershell
.\lake.bat verify-sources
.\lake.bat doctor
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests -q
```

`doctor` 会如实报告数据、日历和模型资产缺项；空安装显示 `partial` 是预期情况。公开源码包的 `release-manifest.json` 列出实际分发文件、大小和 SHA-256，校验失败返回非零退出码。

- [多因子学习、CLI 与模型身份](docs/LEARNING.md)
- [模拟执行规则与局限](docs/EXECUTION_RULES.md)
- [板块行情特征](docs/MARKET_FEATURES.md)
- [统一接口与旧入口迁移](docs/LEGACY_RETIREMENT.md)
- [发行内容、复核与打包](docs/RELEASE.md)

研究数据、当前成分股、复权、公司行为和因子真实可得时间仍需逐项核验。训练完成或历史收益为正不能证明未来盈利、严格历史 PIT 或实盘可用性。

## 目录

```text
src/lake_equity/           数据、学习、模型注册、扫描、回测与 API
frontend/                 当前 Dashboard 与本地图表资源
packages/khquant_research/ 冻结研究引擎、CZSC/Kronos 来源及原生 wheel
vendor/cnequity/           冻结 CNEquity 源码与原许可证
third_party/              补充的第三方许可正文及来源索引
tests/                    隔离的合成数据测试
docs/provenance/          公开来源摘要与依赖身份
var/                      本机行情、模型、账户和运行状态，不公开分发
```
