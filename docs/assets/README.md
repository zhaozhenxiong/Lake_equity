# 界面展示素材

这四张图片选自项目开发期已有的 LakeEquity 界面截图，按原始字节复制，没有重画、裁剪或修改内容。原始文件位于本机运行目录，公开包只包含下表选定的展示副本。

| 公开文件 | 原始文件（相对项目根目录） | 真实格式与原始尺寸 | 拍摄日期与用途 |
| --- | --- | --- | --- |
| [overview.png](overview.png) | `var/validation/ui-20261023/desktop-overview.png` | PNG · 1440 × 1708 | 开发期既有截图；目录标记为 2026-10-23，界面时钟显示 10/23 23:41。展示默认方法、一键流程与覆盖不足时的阻断状态。 |
| [structure-analysis.jpg](structure-analysis.jpg) | `var/validation/screenshots/workbench.png` | JPEG · 1265 × 712 | 开发期既有截图；界面时钟显示 10/22 20:26。展示结构与量价、K 线、笔和中枢。 |
| [market-features.jpg](market-features.jpg) | `var/validation/market-features-production.png` | JPEG · 1265 × 712 | 开发期既有截图，具体拍摄日期未确认。展示板块成交额全景与成交额增减。 |
| [data-coverage.png](data-coverage.png) | `var/validation/unified-models/browser/data-inspect-heatmap.png` | PNG · 1520 × 1000 | 开发期既有截图，具体拍摄日期未确认。展示逐数据集覆盖热图与来源检查。 |

原文件没有可确认拍摄日期的元数据；目录名称和界面时钟仅作来源线索，不作为精确拍摄时间证据。两张原名为 `.png` 的文件实际使用 JPEG 编码，公开副本改用 `.jpg` 扩展名，字节保持一致。

## SHA-256

以下摘要对原始文件和公开副本均适用：

```text
05cb524ddf1155af19cecf2ef5082cef42e0995c9c0090fd262c6f2b564d5621  overview.png
1a35b95932ae582a0acf80b223b70f1a65f4f89f22320d11db064a6a81a61411  structure-analysis.jpg
c038e4b55c3e08595fcb60f31df93b861e13bc0718960c5b4e50b8ffd274f869  market-features.jpg
6b5fe445e2e68d0b5937c81345e58d93daa13f6c418a701612290cbf8001497c  data-coverage.png
```

## 如何理解截图

图片用于说明界面与功能，所显示的日期、覆盖状态、模型身份、任务与研究标记属于当时的开发环境快照。首次安装不附带行情、训练权重、账户或历史研究记录；截图不表示新安装已经具备相同数据和结果，也不构成当前行情、收益或实盘可用性的证明。较早截图的导航名称和布局可能与本版界面存在差别，实际操作以当前程序和文档为准。

选图避开了个人账户、持仓、关注清单和收益曲线页面。结构图中的股票代码与行情、板块图中的成交数据仅为界面示例。LakeEquity 自有界面随项目采用 [MIT](../../LICENSE)，第三方图表组件的许可与归属见 [第三方声明](../../THIRD_PARTY_NOTICES.md)。
