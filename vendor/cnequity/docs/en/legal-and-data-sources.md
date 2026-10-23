# License, sources and citation

This page explains the boundary between the **software license** and **upstream data terms**. Open-sourcing this repository does not mean the market data or announcements you fetch can be freely redistributed.

## Software license

- The source code of this repository is released under the [Apache License 2.0](../../LICENSE) (see [NOTICE](../../NOTICE)).
- You may use, modify, and redistribute the code (meeting the Apache-2.0 obligations for attribution, change notices, NOTICE retention, and so on).

## Data does not ship with the repository

- The Git repository does **not** contain the production data lake (`data/`), local configuration (`configs/cnequity.toml`), or run logs.
- After you run `cne init` / `cne run daily`, the data lands on your own machine (or in the `data.root` you specify). Copyright and usage restrictions for that content are determined by **each data source provider**, not by this project's Apache-2.0 license.

## Upstream sources (summary)

This engine accesses a number of public and semi-public interfaces and files through adapters, including but not limited to:

| Source | Typical use | Notes |
|------|----------|------|
| TDX protocol (built-in client) | Daily bars, indexes, ex-rights, security list, minute bars, trade ticks | Requires a reachable market-data server |
| EastMoney HTTP interfaces | Fund flow, valuation snapshots, corporate actions, structure, etc. | Not an official SDK; rate limits and fields may change |
| Sina and other market-data HTTP | Some fallback sources / snapshots | Same as above |
| Baostock | Valuation and ST history backfill, etc. | Follow its user agreement and access frequency |
| CNINFO | Regulatory filings / announcements | Follow the site's terms of use |
| People's Bank of China, Statistics and Analysis Department | Increment in aggregate financing to the real economy | Official statistical table attachments (xls/xlsx); only the monthly increment column is used |
| Shenwan Research public classification files | Industry classification history | Public XLS; parsing dependencies ship with the base install |
| SSE / SZSE public releases | Trading calendar fallback, ST short-name cross-check, authoritative comparison for `daily_bars`, primary source for `margin_trading` | Compiled and published by the exchanges themselves, more authoritative than any redistributor; the terms on each release page still need to be confirmed per interface |

Prefer the publisher over a redistributor (see [Product scope](architecture/overview.md)): when you can read directly from the institution that compiles the data,
check against the original published evidence first. Source authority and usage permission are judged separately: a public release by an exchange does not by itself imply permission to cache, use commercially, or redistribute. For the restrictions and pending-review status registered in the repository, see the [source matrix](legal-and-data-sources.md#source-compliance-matrix); this documentation cleanup does not constitute a fresh review of upstream terms.

For per-dataset primary sources, fallback sources, and limitations, see [Per-source limitations](datasets/sources.md) and the [dataset catalog](datasets/catalog.md).

### Statement on what "trade ticks" (`trade_ticks`) means

This gets its own section because it is the item most easily mistaken for something this project does not provide:

- TDX trade ticks come from **3-second Level-1 market-data snapshots** and are aggregated. Each record may aggregate several real trades.
- They are **not** exchange tick-by-tick trades, and **even less** tick-by-tick orders or a ten-level order book.
- Timestamp precision is the **minute** (the seconds are always `00`); these are not exchange timestamps.
- `direction` (buy/sell/neutral) is a direction **inferred** by TDX using the tick rule and is not guaranteed to match the exchange's classification.
- Full tick-by-tick / Level-2 data must be purchased from an exchange or a licensed vendor. This project **does not provide, proxy, or circumvent** any such license.

This repository's documentation and code comments all describe the data on the terms above; if you use it downstream as Level-2 data, the risk and compliance responsibility are yours.

## Your responsibilities

By using this software you understand and agree that:

1. **Compliance is on you**: comply with the laws of your jurisdiction and with the terms of service, copyright, and access restrictions (including scraping and commercial-use restrictions) of each upstream website/API/SDK.
2. **No data redistribution license is granted**: the maintainers do **not** grant you the right to redistribute, resell, or publicly host curated Parquet; whether that is allowed depends on the upstream sources, not on this repository.
3. **No availability guarantee**: when upstream redesigns, IP bans, certificates, or rate limits cause failures, the engine should surface the failure rather than fill in fake data; this is not grounds for a defect claim against this project (unless the code itself violates a documented data contract).
4. **Secrets and egress**: proxies, cookies, local paths, and the like belong to your runtime environment; do not commit them to git or attach them to issues.

Upstream terms and compliance boundaries are described above; for implementation details see [Per-source limitations](datasets/sources.md).

## Security issues

Report vulnerabilities privately as described in [SECURITY.md](../../SECURITY.md); do not paste credentials or full local configurations into public issues.

## Relationship to the positioning docs

If you are evaluating "should I use this project or akshare / Tushare": first read [Is it right for me](architecture/overview.md#is-it-right-for-me), then read this page to confirm the data compliance boundary.

## Source compliance matrix {#source-compliance-matrix}
`sources/SOURCES.yml` is the source compliance register, kept separately from the dataset registry. It records only source labels, access methods, terms-review status, and conservative usage conclusions; it does not replace any upstream service agreement and does not grant downstream users any data usage or redistribution license.

### Coverage

The matrix's set of sources comes from the `primary_source`, `backup_source`, and `backfill_source` of each `DatasetSpec` in `src/cnequity/domain/datasets.py`. Fallback sources and sources used only for historical backfill must therefore be registered too. `derived` is a special explicit source label: it marks locally derived results and must not be treated as an independent source of data licensing.

The repository currently registers 15 source labels; the historical review notes below only explain the background of the registrations and do not mean every endpoint or term has been re-verified today. You can confirm that the registry and the matrix are still consistent with the read-only check below:

```python
from cnequity.compliance.source_policy import load_source_policies, required_sources

policies = load_source_policies()
assert required_sources() <= policies.keys()
```

As of 2026-08-29, the matrix records the official user license pages and restrictive conclusions for EastMoney and THS; all other sources remain pending verification.

On 2026-09-13, `ths_official` (the THS official API, fuyao.aicubes.cn) was added. It is a different source label from `ths`: `ths` scrapes public 10jqka pages and is not a registered client, while `ths_official` is a registered client using an API key issued to an account, so `authentication` is recorded as `api_key`. However, the site's documentation (including the full-text aggregate llms-full.txt) contains no terms at all about commercial use, redistribution, caching, or retention of the data, only a single sentence saying that data permissions are governed by the official website and account authorization. `commercial_use`, `redistribution`, and `cache_allowed` therefore all remain `unknown`, and as a result `cne sources policy ths_official` returns `review_required`. The upstream repository's MIT license covers only the code, not the data.

On the same day, `bse` (Beijing Stock Exchange) was registered retroactively. This source is used for the BJ list, status, and current-period market data; `bse_*` sub-labels follow its registered policy.
The BSE site returns 403 from egress outside mainland China and its terms page is unreachable, so all permission fields remain `unknown`.
Sub-labels `bse_*` inherit this policy by prefix, with no adapter changes needed. "Reviewed" here only means the maintainers converted the explicit restrictions on the page into conservative machine states; it is not a complete legal opinion from a lawyer.

### Fields and conservative semantics

Each source contains at least `owner`, `access_type`, `tos_url`, `tos_reviewed_at`, `authentication`, `personal_use`, `commercial_use`, `cache_allowed`, `redistribution`, `rate_limit`, `retained_payloads`, `legal_status`, and `notes`. Unverified facts must be filled in as the exact value `unknown`; they must not be replaced with empty values, guessed dates, or a vague "it's public, so it's allowed".

`personal_use`, `commercial_use`, `cache_allowed`, and `redistribution` are treated as permitted by the usage policy only when explicitly `allowed` (or the boolean `true`); `unknown` always yields a pending-review/blocking result. `tos_reviewed_at` may contain a date only after the corresponding terms have actually been reviewed by a person; the code repository's Apache-2.0 license does not change the restrictions on upstream data.

`policies_for_dataset("daily_bars")` aggregates policies across the primary, fallback, and backfill sources; `usage_profile(...)` makes a conservative risk judgment for personal use, commercial use, caching, or redistribution intents. This API only provides machine-readable risk thresholds and does not constitute legal advice.

### Generation and maintenance process

The matrix is not a list fetched from the network at runtime. When adding or modifying a `DatasetSpec`, maintainers should:

1. Recompute the unique source labels in the registry and fill in all required matrix fields for each new label; `derived` may be marked `true` only when the data is genuinely derived locally.
2. Verify each item against the source's current official terms, interface documentation, license text, and rate-limit/caching rules. Keep anything that cannot be verified as `unknown`; do not infer permission for commercial use or redistribution from an interface not requiring login, a web page being accessible, or another source's license.
3. Update `tos_url`, `tos_reviewed_at`, and `notes` in the same change, stating the scope and date of the review; composite labels (such as `eastmoney_kline+sina_global`) must have every component source reviewed separately.
4. Run the source policy unit tests and `ruff check`. If the policy file is moved to an external path, callers should explicitly run `validate_source_policies` before use and must not bypass validation.
5. When terms change, a source is migrated, a service is discontinued, or permission is withdrawn, revert to `unknown` or record the explicit restriction, and keep a note of the change; do not treat "was once accessible" as current authorization.

This matrix only describes the access methods observed by the repository's implementation and the items pending confirmation. Users must still read the upstream terms themselves, assess the requirements of their jurisdiction, and take responsibility for fetching, storing, commercial use, and redistribution.

## Citing the project
If cnequity has helped your paper, research report, or data engineering, please cite the repository. GitHub reads [`CITATION.cff`](https://github.com/rootSunc/CNEquity/blob/main/CITATION.cff) in the root directory and provides a "Cite this repository" entry on the repository home page.

### Software citation

```text
CNEquity Contributors. (2026). CNEquity: A free, self-hosted historical
financial data infrastructure for China markets, starting with A-shares
(Version <your installed version or commit>). Apache-2.0.
https://github.com/rootSunc/CNEquity
```

For versioned research, also record:

- The `cnequity` version or Git commit
- The data lake's `coverage_start` / `coverage_end`, the dependency revision map, and the research snapshot identity
- The `adjust`, `strict_adj`, profile / `scope_hash`, `strict_universe`, `as_of`, and `pit_mode` semantics
- The upstream data sources and their license restrictions

The software license is Apache-2.0; market data, announcements, and financial statements stored on disk remain subject to upstream terms, and citing the software alone does not grant redistribution rights. See "Software license" and "Your responsibilities" on this page.
