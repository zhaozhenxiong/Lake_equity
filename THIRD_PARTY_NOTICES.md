# Sources and third-party notices

LakeEquity's original integration code is licensed under MIT; see [LICENSE](LICENSE). This does not relicense separately identified third-party code or data. Keep the licenses and notices below when redistributing their respective components.

| Component | License and source information |
| --- | --- |
| CNEquity 0.18.0 | Apache-2.0; `vendor/cnequity/LICENSE` and `NOTICE`, including the upstream tdxpy MIT notice. The original input was a local source archive; its filename is not evidence of an upstream Git commit. |
| KHQuant research snapshot | MIT; `packages/khquant_research/LICENSE`. A snapshot may include local modifications to the recorded upstream revision; it is not represented as a clean upstream checkout. |
| CZSC 1.0.1 | Preserve `packages/khquant_research/app/vendor/czsc/LICENSE`, `czsc-source-manifest.json` and the wheel index. Upstream package/source metadata says Apache-2.0 while the Rust workspace declares MIT; LakeEquity does not resolve this discrepancy by relicensing either. Native wheel bytes remain unchanged. |
| CZSC native dependencies | Additional original license texts and exact-version provenance are collected under `third_party/czsc/`. The collection conservatively covers the Cargo lockfile; it is not a claim that every listed crate is linked into every wheel. See its coverage report for scope. |
| Kronos | MIT; `packages/khquant_research/app/vendor/kronos/LICENSE`. Neural model weights are not bundled and require separate rights and preparation. |
| TradingView Lightweight Charts 4.1.3 | Apache-2.0; `frontend/vendor/lightweight-charts.LICENSE` and `.NOTICE`, also preserved beside the original research assets. The Dashboard retains a visible TradingView attribution and link. |
| Lightweight Charts standalone dependencies | The upstream 4.1.3 metadata pins fancy-canvas 2.1.0 (MIT) and tslib 2.5.2 (0BSD). Supplementary original license texts and source references are in `third_party/fancy-canvas/` and `third_party/tslib/`. |
| Apache ECharts 6.1.0 | Apache-2.0; license and NOTICE remain beside the assets in `frontend/vendor/`. Asset hashes are recorded in `docs/provenance/chart_assets.json`. |
| Other original research frontend assets | Original vendor notices remain with their assets under `packages/khquant_research/app/my_strategy/web_dashboard/static/vendor/`. |
| Microsoft Qlib / pyqlib 0.9.7 | MIT; installed separately in the optional environment, with its distribution license. Windows dependencies are pinned in `requirements-qlib.lock`; the source distribution does not bundle Qlib binaries or change upstream code. |
| LightGBM / scikit-learn / PyTorch and other Python dependencies | Installed separately, not relicensed by the root MIT file. Their installed distributions retain their original licenses. Main dependency versions are pinned in `requirements-runtime.lock`. |

Public source provenance is summarized in `docs/provenance/sources.json`; `release-manifest.json` records the exact distributed bytes. Non-runtime historical documents and private migration/audit evidence are omitted from the public subset. The original frozen numerical engines and native wheel bytes are preserved; no complete upstream Git history is fabricated.

Source-code licenses do not grant rights to upstream market data, account records or model weights. Raw market data, trained models, accounts, credentials, machine configuration and private audit state are excluded from this source distribution.
