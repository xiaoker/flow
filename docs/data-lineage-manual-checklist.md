# FLOW 数据血缘与人工核对表

> 原始样本审计时点：2026-07-16（Asia/Shanghai）；采集规则更新：2026-07-22  
> 待发布快照：`asOf=2026-07-21`，`observedAt=2026-07-22T16:06:53+08:00`  
> 本地原始证据复核 run：`20260722T160650+0800`，交易日 `2026-07-17 / 07-20 / 07-21`

## 技术结论

- 四个页首数值可从保存的交易所原始文件复算：上交所 ETF 份额变化 `-70.11 亿份`、南向净买入 `+71.67 亿港元`、沪深融资余额 `2.6895 万亿元`、北向成交总额 `4,393.51 亿元`。
- 上交所 ETF、深交所 ETF、沪深两融、港交所互联互通的原始字段与单位换算正确。ETF 名称/代码对照表共 `1,893` 行，代码不一致 `0`，名称不一致 `0`，份额换算不一致 `0`。发布级 QA 为 `93 通过 / 0 警告 / 0 失败`。
- 深交所 ETF 已按官方列表页口径修正：页面 `metadata.subname` 是发布日，“当前规模”列标注日是 Excel 数据日，并必须等于前一已知交易日。日期不一致时阻断发布。
- ETF `最近3日` 已修正为必须有 4 个交易日快照后才展示；ETF 分类仍不是官方行业分类，需分开验收。

## 1. 原始数据地址与获取方式

| 数据域 | 官方人工查看页 | 程序实际请求地址 | 获取方式 | 原始格式 | 日期来源 | 原始单位 |
|---|---|---|---|---|---|---|
| 上交所 ETF 份额 | [ETF 规模](https://www.sse.com.cn/market/funddata/volumn/etfvolumn/) | [`commonQuery.do`](https://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=10000&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=2026-07-15) | HTTP GET；`sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`；`STAT_DATE=YYYY-MM-DD`；带上交所 Referer | JSON | `STAT_DATE`，源站发布 | `TOT_VOL`=万份 |
| 深交所 ETF 当前列表 | [基金列表](https://fund.szse.cn/marketdata/fundslist/index.html) | [`ShowReport/data`](https://fund.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1000_lf&TABKEY=tab1&tab1PAGENO=1&tab1PAGESIZE=1) + [`ShowReport`](https://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1000_lf&TABKEY=tab1) | 先读 JSON 页面元数据，再下载 XLSX；带深交所基金页 Referer | JSON + XLSX | `metadata.subname`=列表发布日；`metadata.cols.dqgm` 括号内日期=Excel 规模数据日；校验为前一交易日 | `当前规模(份)`=份；`净值`=元/份 |
| 上交所两融汇总 | [融资融券汇总](https://www.sse.com.cn/market/othersdata/margin/sum/) | [`queryMargin.do`](https://query.sse.com.cn/marketdata/tradedata/queryMargin.do?isPagination=true&beginDate=20260713&endDate=20260715&tabType=&stockCode=&pageHelp.pageSize=5000&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=5) | HTTP GET；起止日 `YYYYMMDD`；带上交所两融页 Referer | JSON | `opDate`，源站发布 | 金额=元；数量=股/份/手 |
| 深交所两融汇总 | [融资融券交易](https://www.szse.cn/disclosure/margin/margin/index.html) | [`ShowReport/data`](https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1837_xxpl&txtDate=2026-07-15&tab1PAGENO=1) | HTTP GET；`CATALOGID=1837_xxpl`；`txtDate=YYYY-MM-DD` | JSON | 请求日期 + 响应 `metadata.subname` | 金额=亿元；数量=亿股/亿份 |
| 港交所互联互通 | [历史每日数据（简体中文）](https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily?sc_lang=zh-CN) | [`data_tab_daily_20260715c.js`](https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_20260715c.js) | HTTP GET；把交易日填入 `data_tab_daily_YYYYMMDDc.js`；带中文 HKEX Referer；解析前移除 UTF-8 BOM | JavaScript 包裹的 JSON | 每个通道对象的 `date` | 汇总表金额=百万本币；前十证券金额=本币元；北向=RMB，南向=HKD |

采集调度：Cloudflare Cron 主触发在北京时间 18:30、次日 07:45、08:45 分时运行，分别覆盖上交所/港交所、深交所晨间发布、SSE 同日 PCF 归档和完整日期核验；GitHub Actions 在 18:45、08:00、08:55 错峰故障转移。每次先通过上交所两融数据发现最近 3 个交易日，再下载上述原始文件；每个工件单独按来源和日期幂等写入 R2 检查点，后续窗口只补缺失工件。某个官方源尚未发布或失败时，已到达的证据仍归档，但不生成完整快照；只有全部核心源确认后才发布。每次请求保存 HTTP 状态、Content-Type、参数、观测时间、SHA-256、原始文件和规范化 CSV。发布时再复算 SHA-256；宽 PCF 规范化 CSV 仅在传输层 gzip 压缩，Worker 校验解压后的原文哈希后以普通 CSV 归档。`market-data/` 原始证据和历史快照进入 R2 并长期保留，D1 ETF 明细滚动保留 365 天、最新页面派生快照保留 30 天，历史快照正文只在 R2 保存。PCF 只采集份额发生变化的 ETF，最多 800 只、4 路并发；个股穿透只接受同一交易日 PCF，缺失或日期不一致即标记不可用，不回退到其他日期。

## 2. 页面字段→原始字段对照

| 页面位置 | 页面名称/内部字段 | 原始字段 | 计算或转换 | 页面单位 | 日期口径 | 当前判定 |
|---|---|---|---|---|---|---|
| 页首 | `截至 / asOf` | 各域请求日期的最大值 | `requested_dates[-1]` | 日 | 全局日期 | ✓；深市 ETF 仍使用自身规模日字段单独展示 |
| 页尾 | `观测于 / observedAt` | 采集 manifest `finished_at` | 不转换 | ISO 8601 | 采集完成时间 | ✓ |
| 页尾/API | `gate`、`quality.pass/warn/fail` | 质量规则结果 | 规则数量汇总 | 项 | 本次采集 run | ✓，但不代表业务含义全部正确 |
| 页首 ETF | `ETF 份额变化` | SSE `SEC_CODE`, `TOT_VOL`, `STAT_DATE` | 同代码当日份额−前一快照份额，合计后 `/1e8` | 亿份 | `07-15 - 07-14` | ✓ 值正确；⚠️ 标题应写“上交所 ETF” |
| ETF 摘要 | `增加只数 / 减少只数` | SSE `TOT_VOL` | 同代码份额差 `>0 / <0` 计数 | 只 | `07-15 - 07-14` | ✓ |
| ETF 排名 | `etfFlows.inflow/outflow` | SSE `SEC_CODE`, `SEC_NAME`, `TOT_VOL` | 份额差降序/升序各取 5 | 亿份 | `07-15 - 07-14` | ✓，不是金额流入/流出 |
| ETF 雷达 | `基金代码` | SSE `SEC_CODE`；SZSE `基金代码` | 左补零为 6 位 | 代码 | 同所属快照 | ✓，1,893 行无代码不一致 |
| ETF 雷达 | `基金名称` | SSE `SEC_NAME`；SZSE `基金简称` | 原样保留 | 文本 | 同所属快照 | ✓，1,893 行无名称不一致 |
| ETF 雷达 | `规模（亿份）` | SSE `TOT_VOL`；SZSE `当前规模(份)` | SSE `TOT_VOL / 10,000`；SZSE `当前规模 / 1e8` | 亿份 | SSE `STAT_DATE`；SZSE 列表页规模列日期 | ✓ 换算正确；⚠️ 名称建议改为“份额（亿份）” |
| ETF 雷达 | `最近1日 / periods.1d` | SSE `TOT_VOL` | `07-15 份额 - 07-14 份额` | 亿份 | 1 个相邻交易日区间 | ✓ |
| ETF 雷达 | `最近3日 / periods.3d` | R2 日变化状态中的 SSE `TOT_VOL` 相邻日差 | 最近 3 个交易日的每日份额变化相加 | 亿份 | 3 个日变化值 | ✓；全局日期覆盖不足时不开放该周期，单只覆盖不足时保持空值 |
| ETF 雷达 | `覆盖 periodCoverage` | 代码在所选周期中的非空日变化 | 非空日变化数 | 个交易日变化值 | 所选周期 | ✓ |
| ETF 雷达 | `主分类 category` | SSE `ETF_TYPE`、SZSE `基金类别/投资类别`、两市基金简称 | v3：严格排除非 ETF；资产类别用官方字段/代码规则优先；股票分类使用顺序化名称规则 | 枚举 | 最新元数据 | ⚠️ 股票行业/宽基仍是中置信度推导，待补跟踪指数代码 |
| ETF 详情 | `assetClass` | SZSE `投资类别` + 简称 | 规则映射 | 枚举 | 最新元数据 | ⚠️ 推导字段 |
| ETF 统计 | `universe.total / marketCounts` | 三日 SSE 代码并集 + SZSE 最新规模日列表 | 去重计数 | 只 | 混合历史范围 | ⚠️ SSE 历史并集可包含最新日已缺失产品，“当日存量”应另用最新日集合 |
| ETF 板块 | `groupPeriods` | R2 哈希校验的 ETF 日变化状态 + v3 `category/includeInGroup` | 每日官方份额变化逐日累计；合格 ETF 全量计入，低置信度“其他”进入“未归类”桶；输出组件数、可比数和各周期覆盖日数 | 亿份 | 最近 260 个交易日；当前历史变化主要来自 SSE | ⚠️ 深市缺少历史变化时保持空值，不补造 |
| ETF 板块 | `拥挤度` | 正变化 `groupPeriods` | 前 3 正变化分类合计 / 全部正变化分类合计 | % | 当前区间 | ⚠️ 是页面派生指标，不是市场标准拥挤度 |
| 互联互通 | `沪港通/深港通南向` | HKEX `market`, `Buy Turnover`, `Sell Turnover` | 通道名硬映射；`(Buy-Sell) × 1e6 / 1e8` | 亿港元 | HKEX `date` | ✓ |
| 互联互通 | `南向净买入` | 两个南向通道 Buy/Sell | `Σ(Buy-Sell) / 100`，原表为百万港元 | 亿港元 | HKEX `date` | ✓ |
| 互联互通 | `南向成交额` | `Total Turnover` | `ΣTotal / 100` | 亿港元 | HKEX `date` | ✓ |
| 互联互通 | `沪股通/深股通北向成交` | `market`, `Total Turnover` | 通道名硬映射；`Total / 100` | 亿元 | HKEX `date` | ✓，不是净流入 |
| 互联互通 | `北向 ETF 成交` | `ETF Turnover` | 两北向通道合计 `/100` | 亿元 | HKEX `date` | ✓ |
| 互联互通 API | `northboundTradeCount` | `Total Trade Count` | 两北向通道加总 | 笔 | HKEX `date` | ✓，当前页面未显示 |
| 两融 | `融资余额 balance` | SSE `rzye`；SZSE `jrrzye` | `SSE元 + SZSE亿元×1e8`，页首 `/1e12`，图表 `/1e8` | 万亿元/亿元 | 各所交易日 | ✓ |
| 两融 | `较前日 change` | 融资余额 | 当日沪深合计−前一日沪深合计 | 亿元 | 相邻交易日 | ✓ |
| 两融 | `融资买入额 financingBuy` | SSE `rzmre`；SZSE `jrrzmr` | 两市加总 `/1e8` | 亿元 | 当日 | ✓ |
| 两融 | `融券余额 lendingBalance` | SSE `rqylje`；SZSE `jrrjye` | 两市加总 `/1e8` | 亿元 | 当日 | ✓ |
| 两融 API | `totalBalance` | SSE `rzrqjyzl`；SZSE `jrrzrjye` | 两市加总 `/1e8` | 亿元 | 当日 | ✓，当前页面未单独显示 |
| 两融 API | `lendingSellVolume` | SSE `rqmcl`；SZSE `jrrjmc` | 两市加总 `/1e8` | 亿股/亿份/亿手的混合数量 | 当日 | ⚠️ 不宜简写为单一“亿股” |
| 两融 | `sseBalance / szseBalance` | 各所融资余额 | 分市场 `/1e8` | 亿元 | 当日 | ✓ |

## 3. 最新值复算样例

| 页面值 | 原始值 | 复算 | 结果 |
|---|---|---|---|
| 沪港通南向 `+13.09 亿港元` | Buy `41,915.35`、Sell `40,606.70`（百万港元） | `(41,915.35-40,606.70)/100` | `13.0865` → `13.09` ✓ |
| 深港通南向 `+58.59 亿港元` | Buy `27,409.86`、Sell `21,551.14`（百万港元） | `(27,409.86-21,551.14)/100` | `58.5872` → `58.59` ✓ |
| 北向成交 `4,393.51 亿元` | SSE `202,221.24`、SZSE `237,129.95`（百万元） | `(202,221.24+237,129.95)/100` | `4,393.5119` → `4,393.51` ✓ |
| 沪深融资余额 `2.6895 万亿元` | SSE `1,363,597,390,293 元`；SZSE `13,259.24 亿元` | `(1,363,597,390,293 + 13,259.24×1e8)/1e12` | `2.689521390293` → `2.6895` ✓ |
| 上交所 ETF 份额变化 `-70.11 亿份` | SSE 880 只同代码 `07-21 TOT_VOL - 07-20 TOT_VOL` | `Σ差额×10,000/1e8` | `-70.11` ✓ |

## 4. 当前不一致与风险

| 等级 | 问题 | 证据 | 影响 | 建议的验收标准 |
|---|---|---|---|---|
| 已解决 | 深交所 ETF 日期曾只使用任务参数 | 官方列表 JSON 同时提供页面发布日和规模列日期；上市日期不是规模统计日期 | 旧快照不可用于日期校验 | 新任务必须同时获取 JSON 元数据和 XLSX，且“规模列日=前一已知交易日”自动测试通过 |
| 已解决 | `最近3日` 曾只有 3 个快照、2 个日变化 | 新周期锚点规则要求 4 个快照 | 不再把两个区间误认成三日累计 | 覆盖不足时周期保持不可用 |
| 中 | ETF 页首、板块和走势未在主标题显示“仅上交所” | 变化值只来自 SSE；SZSE `periods` 全为空 | 容易被理解为沪深全市场 ETF | 所有变化标题明示“上交所”，直到深市实际积累出相邻快照 |
| 中 | ETF 存量使用三日代码并集 | 当产品在观察窗口内新上市或退出时，并集不必等于最新日产品数；本次 1,893 行对照中最新沪市缺失为 0 | 未来某日的“总数”可能混入历史产品 | 存量只使用同日最新行；历史退出代码单独标记 |
| 中 | 股票“主分类/板块”仍未接入跟踪指数代码 | v3 已排除 LOF，并修复科创债、科创医药、科创宽基、港股科技等优先级污染；未匹配产品不再进入汇总 | 已知误分显著下降，但名称相近的新产品仍可能漂移 | 采集跟踪指数代码和指数分类，用代码级映射覆盖名称规则 |
| 中 | 生产 D1 快照契约仍是旧版 | 最新生产快照 `observedAt=14:16:19`，`metrics[].basis` 缺失，数据状态仍是“当前快照/沪深完整/4通道”；新 UI 依靠前端默认规则补标签 | UI 和 API/D1 证据字段不同步 | 用新版生成器重新采集并发布，发布后断言四个 `basis.kind` 全部存在 |

## 5. ETF 名称/代码全量人工勾选表

全量 CSV：[`reports/data-lineage/etf-name-code-check.csv`](../reports/data-lineage/etf-name-code-check.csv)

每行包含页面代码/名称、原始字段名、原始代码/名称、原始份额、页面亿份值、日期基础、官方上市日、自动比对结果和空白 `manual_check` 列。可用 Excel 筛选：

1. `code_match/name_match/shares_match = FALSE`；
2. `latest_source_status != 当日官方行`；
3. `date_basis = source_page_previous_trading_day`，并核对 `source_page_date / source_scale_date`；
4. `official_listing_date > 页面快照暂填日期`。

## 6. 建议的人工验收顺序

1. 先在深交所列表 JSON 中核对 `metadata.subname`、`metadata.cols.dqgm` 的日期和最近交易日列表。
2. 下载上表 5 个原始数据，使用“最新值复算样例”独立计算四个页首数值。
3. 打开 ETF 全量 CSV，先筛选异常行，再抽样沪市/深市各 20 只核对代码、简称、份额和上市日。
4. 对“科技/宽基/医药……”分类单独验收，不要把代码名称正确当作分类正确。
5. 修正高等级问题后，重新发布 D1 快照，再从 `/api/v1/overview`、`/api/v1/etf/flows`、`/api/v1/connect/flows`、`/api/v1/margin/flows` 各抽查一次。

## 开放问题

- ETF 主分类是采用“官方标的指数分类”、“中证/国证行业”，还是保留当前的个人主题分组？
