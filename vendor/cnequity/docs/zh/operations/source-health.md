# 数据源健康度：这张表是怎么来的

这些源不是本项目专属的——AkShare、各类取数 skill、你自己写的爬虫，走的是同一批端点。其中某一个变了，通常没有地方可查，你得先花半天怀疑自己的代码。CLI 用小范围探测记录当前出口的可达性，结果带时间和出口标签。

## 怎么用

```bash
cne sources probe --list           # 离线查看合法名称
cne sources limits                 # 离线检查共享冷却和当日预算
cne sources probe --only sina --vantage cn  # 显式测一个端点
cne sources probe --stale-only --vantage cn  # 日更后只补探普通端点；高风险端点默认跳过
cne serve                    # 打开控制台后选择「数据源」页面
```

**探测在 CLI，展示在 serve。** 健康页面只展示已有报告——它不会替你去请求十几个第三方主机，和它不触发采集是同一个理由：一个无鉴权的本地服务，不该能被一个走神的浏览器标签页指向别人的接口。

只测某几个源；一次只选择当前需要诊断的源族：

```bash
cne sources probe --only cninfo
```

`--vantage` 是**必须认真填**的：它记录这次探测从哪个出口发出去。见下面「为什么视角决定结论」。每个 vantage 一个文件，页面把它们并排渲染。

## 五个状态

| 状态 | 含义 |
|------|------|
| **可用** | 返回了真实数据 |
| **空响应** | 连上了、HTTP 也正常，但没有数据 |
| **被拒** | 到达了但被拒绝（403 / 风控页 / challenge） |
| **不可达** | 连不上或超时 |
| **未探测** | 配置里关了，或被 `--only` 排除 |

### 为什么「空响应」单独一档

**HTTP 200 不等于可用。** 东财会用 200 返回风控页，新浪会用 200 返回空数组，同花顺限流时同样是 200 加空响应。只看状态行的探测会把这三种都判成健康。

所以每个探测都断言**响应体**：clist 要有 `total`、kline 要有 `klines`、深交所导出要以 `PK` 开头（xlsx 是 zip；风控页是 HTML）、申万下载要以 OLE2 magic 开头。

「空响应」被单独拎出来，是因为它看起来比失败更健康，实际更危险——回填会静默截断，而且从外面看不出来。

### 为什么「被拒」不算「挂了」

「拒绝了你」和「它不在那儿」指向完全不同的修法。前者先停止同源请求并查看冷却/鉴权状态；后者还需检查路由、超时和源服务。不要用换 IP 重试来判断原因。

## 为什么视角决定结论

同一主机在不同网络路径、出口和时刻可能有不同结果。一次失败不能推断整个地域不可用，也不能仅凭 502 或空回复断言 IP 被封。先保留状态和时间，按[源保护](fetch-policy.md)冷却与诊断。

所以每份报告带 `vantage` 标签，页面把不同视角**并排**放，不合并成一个结论——合并等于凭空造一个哪次探测都没测到的「事实」。

在两个网络里各跑一次，页面就有两列：

```bash
cne sources probe --stale-only --vantage cn          # 大陆出口
cne sources probe --stale-only --vantage overseas    # 海外出口
```

文件名不决定列名，JSON 里的 `vantage` 字段才决定。同名会覆盖，所以同一个出口重复跑就是刷新那一列。

## 一次探测不是 SLA

每个源执行有界探测，部分源需要握手、分页或下载多个文件。交易所探针最多尝试两个工作日：上期所、郑商所、广期所、大商所只验证期货日文件，独立期权文件须由真实采集校验；中金所的单份文件同时包含期货和期权。Baostock 登录、查询、退出也分别占用请求。探测通过只说明此次请求和响应校验成功，不代表大范围抓取能达到相同成功率。它共享采集的限流、冷却与已有熔断，不应高频轮询。`--only` 名称拼错或空白会在网络请求前报错。

`dce`、`ths_pages`、`baostock` 有已知挑战或累计请求成本；`cni` 下载完整历史文件，也属于高成本探针。默认和 `--stale-only` 全表探测把它们标成「未探测」，但可复用同一出口最近 12 小时内的真实采集校验证据。只有排除同源任务冲突、看过 `sources limits` 后，才用 `--only dce` 等逐个显式诊断；遇到拒绝就停止，不连续试探冷却时长。它们的主动探针不作为核心 SLO 的强制采样门禁。
探测是**串行**的。这些正是日更流水线依赖的主机，十几个请求一起打出去，是健康检查自己制造它本该观测的故障。

## 探测走的是适配器自己的代码

URL 常量、东财的鉴权头、上交所需要的 Chrome TLS 伪装、同花顺的限速、TDX 的二进制协议——用的都是流水线在用的那套。适配器改了，探测跟着改；探测绿而流水线红这种情况，不会因为两边各写一份 URL 而发生。

反过来也成立：探测所需的日期用的是**三天前的最近工作日**，不是今天。好几个端点在收盘前没有当日数据，每天早上飘红的表会被训练成没人看。

## 报告存在哪

`{data_root}/meta/source_health/<vantage>.json`，和湖的其它元数据放在一起。`cne serve` 启动时不读，访问数据源健康 API 时才读——所以先跑探测再刷新页面即可，不用重启。

**本地没有强制的定时发布。** 想每天自动跑就挂进你现有的调度里（见 [runbook](runbook.md)）：

```bash
cne sources probe --stale-only --vantage cn >> logs/source-health.log 2>&1
```

**探测失败不会让命令失败。** 源变红是这条命令的**输出**而不是它的错误；如果调度需要门禁，请解析 JSON 中的 `status`，按业务决定是否阻断日更。

仓库还提供一个工作日关键源探测、每周普通端点探测的 GitHub Actions workflow：它从海外 runner
探测 TDX、北交所首分页、Sina、东财和巨潮，
把文本摘要写入 Job Summary，并上传 JSON artifact。这个报告只代表
`github-actions` 视角；大陆机器可运行 `cne sources probe --stale-only --vantage cn`，不要把海外的 `blocked`
误读成全局故障。

该 workflow 会用唯一 run key 恢复并保存 `meta/source_health` 的 Actions cache，因此 30 日
SLO 使用的是跨运行的不可变样本，而不是临时 runner 内的一次探测。每次报告同时作为 30 天
artifact 留存；cache 丢失时 SLO 会因样本不足 fail-closed，不会把空历史解释为健康。

## Source SLO 与韧性

`cne sources probe` 默认同时写入 latest 与不可变的 vantage 历史样本；显式 `--out` 只用于一次性
导出。累计样本后运行：

```bash
cne sources slo --window-days 30 --minimum-observations 10 --enforce
cne sources resilience --enforce
```

主动探测与真实采集的被动证据分别计数；样本足够时优先主动探测，否则使用被动证据，不把两类样本混成一个成功率。第一条按 probe/vantage 分开计算，每个 UTC 日只采用最后一次非跳过探测；跳过项不算失败，
关键源缺最小跨日样本或样本过期均失败。同日重试可验证恢复，但不能用来刷高 SLO 样本数。
连续三次失败会写入稳定 `dedupe_key` 的 `meta/source_health/incidents.json`；后续 CI 重跑更新
同一事故载荷。第二条从 DatasetSpec 生成集中度与 failure-domain 爆炸半径，并验证核心数据集
是否有真正独立的备源。复权因子属于研究层：Sina 单源风险会被报告，但其失败将 run 标为
`degraded`，不会把已提交的原始 daily bars revision 回滚或误判为核心失败。



## 相关文档

- [CLI](../reference/cli.md#cne-sources) · [产品边界](../architecture/overview.md) · [逐源限制](../datasets/sources.md) · [故障排查](troubleshooting.md)

## 可用率目标按 vantage 分档

可用率按 **(源, 网络出口)** 分别统计。下面是当前客户端门禁策略，不是数据供应商的 SLA，也不是对各地域可用性的保证。实际依据为已保存的跨日样本：

| vantage 类 | critical 目标 | 其他 |
|---|---|---|
| `cn` | 99% | 95% |
| `overseas` | 90% | 85% |
| 未声明 | 按 `cn` 算 | |

分类只看 `CNE_SOURCE_VANTAGE` 的**前缀**，不认地名：`cn`、`cn-sh`、`cn_aliyun` 是大陆；
`overseas`、`overseas-eu`、`overseas_aws` 不是。地名从来不是判据。

**未声明的 vantage 按严格档处理** —— 门禁不该给没人声明的出口发折扣，而声明它只需要一个环境变量。

不同档位是运维策略；缺少样本、失效证据或关键源持续失败仍须处理，不能通过换标签把不可用的源标成健康。个人测量保留在本地运行记录中。
