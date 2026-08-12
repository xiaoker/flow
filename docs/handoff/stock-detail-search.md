# Flow 个股资金穿透页与统一搜索实施交接

> 交接对象：GPT-5.6 Luna（或后续实现者）
> 编写日期：2026-08-11
> 仓库：`xiaoker/flow`
> 生产地址：`https://flow.xiaoker.com`
> 状态：已按本文落地（2026-08-11）；本文继续作为产品、数据与工程验收合同

## 1. 先读结论

本次要实现两条入口，最终进入同一个“个股资金穿透页”：

1. 顶部统一搜索：输入个股名称或代码，选择结果后直接进入个股页。
2. ETF 雷达：打开 ETF 详情，点击“ETF → 个股”列表中的个股后进入同一个个股页。

本页展示的是 **ETF 申赎篮子对应的个股等价变化估算**，不是二级市场真实资金流。任何 UI、字段、图例、导出和说明都不得把它简称为“个股资金净流入”。

第一版必须完成：可搜索、可直达、可从 ETF 下钻、可复制 URL、可查看当日贡献 ETF 与数据凭据。个股历史折线和个股融资融券不属于第一版的强制范围；没有真实且日期对齐的数据时必须显示覆盖不足，不得补造。

## 2. 开始实施前的仓库基线

实施前必须先运行：

```bash
git status --short
git log -5 --oneline
```

本文编写时，`main` / `origin/main` 指向：

```text
5841406 fix: read active release manifest from manifest table
1dd76ea fix: keep ETF drilldown read model bounded
ef1d6c3 fix: expose ETF-first stock drilldown
```

最近一次已验证的生产发布基线为：

```text
asOf: 2026-08-10
releaseId: release:2026-08-10:fd5cccfe210a5b5e
```

这只是交接时的观测值，不得写死进代码。实施和验收时应通过已认证的 `/api/v2/releases/latest` 重新读取当前发布指针。

本文编写时工作树还有用户已有、与本任务无关的改动：

```text
M  reports/phase0/latest-manifest.json
M  reports/phase0/latest-quality-summary.json
M  reports/phase0/latest-release-qa.json
?? memory/
```

这些文件不属于本任务。不要覆盖、还原、暂存或提交它们。

## 3. 必须先阅读的现有合同

实现者应先阅读：

- `docs/flow-runtime-architecture.md`
- `docs/data-quality-contract.md`
- `docs/data-lineage-manual-checklist.md`
- `src/trading_desk/etf_stock_flow.py`
- `src/trading_desk/read_models.py`
- `web/app/lib/release-store.ts`
- `web/app/dashboard-bootstrap.tsx`
- `web/app/dashboard.tsx`
- `web/app/api/v1/admin/read-model-import/route.ts`
- `web/app/api/v2/views/[view]/route.ts`
- `.github/workflows/daily-data.yml`

不得破坏以下架构原则：

```text
官方来源
  → GitHub Actions 采集、QA、离线计算
  → R2 不可变发布对象
  → D1 release_manifest / active_release 原子切换
  → Worker 只读取并流式返回
  → 浏览器展示
```

Worker 请求路径不得读取全量 ETF 滚动状态、现场计算 ETF→个股关系、聚合几千只个股或解析大型发布对象。此前出现的 Cloudflare Error 1102 就与请求路径资源消耗过大有关。

## 4. 当前已经具备什么

### 4.1 当前计算

`src/trading_desk/etf_stock_flow.py` 已实现离线计算：

```text
个股篮子等价变化股数
= ETF 官方份额变化 ÷ ETF 最小申赎单位 × PCF 成分数量
```

当前结果包含：

- `basis.shareDate`
- `basis.previousShareDate`
- `basis.pcfDate`
- `basis.pcfAlignment`
- `coverage`
- 全局前 500 个股 `stocks`
- 每只匹配 ETF 的前 12 个股 `etfs[].stocks`
- 个股的主要贡献 ETF `contributors`

当前 ETF 详情抽屉已经可以展示每只 ETF 的前 8 个股，底层每只 ETF 保留前 12 个股。之所以限制为 12，是因为前 30 个股曾令 `radar.json` 超过单工件 5 MB 上限，发布失败。

### 4.2 本次已落地的页面与搜索能力

- 顶部统一搜索已经与 ETF 雷达筛选状态分离，结果按“个股 / ETF”分组。
- 个股支持代码精确优先、名称包含匹配、上下键、Enter、Escape 和鼠标选择。
- ETF 详情中的个股贡献行已经可以进入同一个独立个股页。
- 个股页使用 `view=stock&venue=<market>&code=<code>&date=<date>` 稳定 URL，可复制、刷新和返回。
- 全量搜索索引不再从 ETF 雷达前 500 或单 ETF 前 12 行反推，而是由同一份日期校验后的 PCF/份额明细离线生成。
- 当前 D1 两融历史仍是市场级汇总；个股页没有冒充个股级融资融券数据。

### 4.3 当前发布限制

`web/app/api/v1/admin/read-model-import/route.ts` 当前限制：

- 单个解压后工件不超过 `5_000_000` 字节。
- 单次上传请求体不超过 `1_500_000` 字节。
- manifest 最多 20 个工件。
- 工件名只接受固定白名单：9 个核心视图（含按需读取的 `radar_flow`）、`stock_search` 和 `stock_[0-7]`。

现有核心发布有 9 个工件；启用个股穿透后共 18 个只读工件：

```text
bootstrap / overview / radar / radar_flow / rotation / national / connect / margin / pressure
```

```text
stock_search / stock_0 / stock_1 / stock_2 / stock_3 / stock_4 / stock_5 / stock_6 / stock_7
```

完整个股图不进入 `radar.json` 或 `bootstrap.json`。

## 5. 数据口径合同

### 5.1 个股唯一键

个股唯一键必须是：

```text
listing_market + stock_code
```

不能使用当前记录里的 `market + stock_code` 作为个股主键，因为 `market` 表示贡献 ETF 所属市场，而 `listing_market` 才表示成分证券上市市场。

现有官方上市市场代码包括：

| `listing_market` | 含义 | 代码规范 |
| --- | --- | --- |
| `101` | 上交所 | 6 位数字 |
| `102` | 深交所 | 6 位数字 |
| `103` | 港交所 | 5 位数字 |
| `104` | 北交所 | 6 位数字 |
| `9999` | 境外市场 | 保留原始标识 |

其他市场若后续接入，必须先在规范层增加明确枚举和测试，不能靠股票代码首位猜市场。
当前源数据缺失上市市场时，只对无歧义的六位 A 股代码做保守推断；港股和其他无法确认的代码标记为“待核验市场”，不与已知市场合并。

### 5.2 日期对齐

只有以下两种 PCF 对齐可以计算：

1. `pcfDate == shareDate`；标记 `same_trading_day`。
2. `pcfDate > shareDate` 且 PCF 官方 `PRE_TRADING_DAY == shareDate`；标记 `next_day_previous_trade_date`。

其他日期组合一律 `available=false`，不得回退到最近可用 PCF。

个股页必须同时显示：

- ETF 份额日
- 前一可比份额日
- PCF 发布日
- PCF 对齐方式
- 计算公式
- 现金替代提示
- 覆盖 ETF 数、覆盖个股数和截断情况

### 5.3 数值语义

允许展示：

- `netShares`：全部匹配 ETF 对该个股的篮子等价变化股数之和。
- `inflowShares`：正向贡献之和。
- `outflowShares`：负向贡献绝对值之和。
- `etfCount`：贡献 ETF 数量。
- 每只贡献 ETF 的 `stockFlowShares`、ETF 份额变化、最小申赎单位和 PCF 数量。

页面中文统一使用：

- “ETF 篮子等价变化”
- “ETF 穿透估算”
- “贡献 ETF”
- “估算股数”

不得使用：

- “个股真实资金流入”
- “主力买入”
- “真实持仓增加”
- “北向流入该股”

当前没有同日、同代码、官方可追溯的个股收盘价，因此第一版不要展示“亿元资金流”。后续只有接入可靠价格源后，才可以计算：

```text
估算金额（亿元）= ETF 篮子等价变化股数 × 同日收盘价 ÷ 1e8
```

且必须标记“估算金额”，不能标记为真实成交资金。

### 5.4 搜索覆盖范围

第一版搜索索引来自当期通过日期门禁并成功匹配的官方 PCF 成分证券，不是完整 A 股证券主数据。

搜索结果和空状态应明确：

- “ETF PCF 覆盖个股”表示当前存在穿透数据。
- 搜不到不能解释为股票不存在，只能解释为当前发布版本没有覆盖。
- 名称优先使用官方 PCF 名称；名称缺失时显示代码并标记名称缺失。
- 不要为缺名个股维护一套复杂证券主数据同步；只有经过人工核验的极少数例外才允许进入小型覆盖映射。

新上市、退市和名称变化由每个发布版本重新生成索引自然处理；历史发布对象保留当时的官方名称。

## 6. 目标交互

### 6.1 统一搜索

把顶部搜索从“ETF 表格筛选器”升级为真正的全局搜索入口，同时把 ETF 雷达内部筛选状态与全局搜索状态分离。

输入名称或代码后，下拉结果按以下顺序展示：

```text
个股
  沪硅产业   688126   上交所   ETF PCF 覆盖

ETF
  KC半导体   589020   上交所   科技
```

交互要求：

- 支持代码精确匹配、名称包含匹配。
- 精确代码结果优先于名称模糊结果。
- 结果按“个股 / ETF”分组，不把同代码不同市场合并。
- 支持方向键、Enter、Escape 和鼠标选择。
- 搜索请求不写 D1；索引随发布对象在浏览器读取并本地过滤。
- 输入法组合期间不得错误触发 Enter。
- 个股结果进入个股页；ETF 结果进入 ETF 雷达并打开对应 ETF 详情。
- 能搜到但该日估算不可用时仍允许进入详情，页面展示不可用原因，不显示 0。

### 6.2 稳定 URL

推荐 URL：

```text
/?view=stock&venue=SSE&code=688126
/?view=stock&venue=SSE&code=688126&date=2026-08-10
```

要求：

- `venue` 与 `code` 共同构成路由身份。
- URL 可复制、刷新和直接打开。
- 进入个股页使用浏览器历史 `pushState`，返回键能回到来源页面。
- ETF 下钻时可在 history state 中保留来源 ETF；不要依赖它完成数据查询。
- 非法市场、非法代码或不存在的当前版本条目展示明确 404 状态。
- 指定 `date` 时只能读取同一日期的发布对象；不得用最新个股数据覆盖历史页面。

### 6.3 个股页布局

建议使用独立页面，而不是在 ETF 抽屉上再叠一层抽屉。

页面从上到下：

1. 标题区：名称、代码、上市市场、数据日期、返回入口。
2. 证据条：份额日、PCF 日、对齐方式、公式计算/估算标签。
3. 核心指标：篮子等价净变化、正向贡献、负向抵消、贡献 ETF 数。
4. 贡献 ETF 表：ETF 简称、代码、ETF 市场、份额变化、PCF 数量、估算股数、占净变化比例。
5. 口径说明：现金替代、覆盖和截断信息。
6. 历史区域：只有至少两个真实发布日节点才画折线；否则显示“历史节点积累中”。
7. 融资融券区域：第一版不展示个股数值；没有个股级官方数据时明确标注“尚未接入”，不要使用市场汇总替代。

正负颜色沿用现有产品约定：A 股语境中红色为增加、绿色为减少。正向/负向之外不要用颜色暗示买卖建议。

## 7. 推荐发布模型

### 7.1 不采用单个巨型 `stocks.json`

完整个股和贡献 ETF 如果塞入一个对象，会接近或超过 5 MB，也会让浏览器每次只看一只股票却下载全部明细。

推荐增加：

```text
stock_search.json
stock_0.json
stock_1.json
...
stock_7.json
```

分片规则必须确定且可测试，例如：

```text
shard = int(sha256("<listing_market>:<stock_code>")[0:8], 16) % 8
```

`stock_search.json` 只包含搜索和定位所需的紧凑字段；选择结果后再加载对应分片。

这会让总工件数从 7 增至 16。应把 manifest 工件上限从 8 调整到一个保守固定值，例如 20，并允许受控的数字后缀。不要放开成任意名称或任意数量。

每天新增约 9 个 R2 写对象，个人使用场景远低于现有免费额度量级；但仍应保留对象不可变、内容寻址和体积门禁。

### 7.2 搜索工件建议结构

```json
{
  "schemaVersion": 1,
  "asOf": "2026-08-10",
  "basis": {
    "shareDate": "2026-08-10",
    "pcfDate": "2026-08-11",
    "pcfAlignment": "next_day_previous_trade_date"
  },
  "coverage": {
    "stockCount": 6328,
    "matchedEtfCount": 491
  },
  "items": [
    {
      "listingMarket": "101",
      "venue": "SSE",
      "code": "688126",
      "name": "沪硅产业",
      "shard": "stock_3",
      "available": true
    }
  ]
}
```

示例数量来自交接时验证，不得写死。

### 7.3 分片工件建议结构

```json
{
  "schemaVersion": 1,
  "asOf": "2026-08-10",
  "items": [
    {
      "listingMarket": "101",
      "venue": "SSE",
      "code": "688126",
      "name": "沪硅产业",
      "netShares": 100,
      "inflowShares": 100,
      "outflowShares": 0,
      "etfCount": 1,
      "contributorCount": 1,
      "shownContributorCount": 1,
      "contributorsLimited": false,
      "contributors": [
        {
          "etfMarket": "SSE",
          "etfCode": "589020",
          "etfName": "KC半导体",
          "shareDelta": 3000000,
          "quantity": 100,
          "creationUnit": 3000000,
          "stockFlowShares": 100,
          "cashSubstitution": false,
          "estimated": true
        }
      ]
    }
  ]
}
```

上面的数值只是结构示例；构建时必须来自当期计算结果。贡献 ETF 可以按绝对贡献降序截取，但必须同时保留总贡献数、已展示数和截断标记，不能让截断列表冒充完整合计。

### 7.4 生成方式

不要从当前 `etfStockFlow.stocks` 的前 500 行反推完整索引，也不要从 `etfs[].stocks` 的每 ETF 前 12 行反推，因为都会漏股票。

正确做法是在 GitHub Actions 离线阶段，使用同一份已经日期校验的 ETF 份额和 PCF `joined` 明细，同时生成：

- 现有有界 ETF 雷达模型。
- 完整个股搜索索引。
- 完整个股分片详情。

建议把 `src/trading_desk/etf_stock_flow.py` 的公共规范化与聚合逻辑抽成一个纯函数，分别输出 radar 和 stock artifacts，避免复制公式。

不要让完整个股 sidecar 先进入 `dashboard-snapshot.json` 再拆分，因为核心快照也要经过 Worker 上传，可能重新触发大小和 CPU 问题。可以让构建脚本输出单独 sidecar，再由 `scripts/build_read_models.py` 一并纳入 manifest。

## 8. API 与发布一致性

### 8.1 上传端

`scripts/publish_read_models.py` 已经按 manifest 遍历工件，可以继续使用；需要同步调整服务端 manifest 校验：

- 工件总数使用固定上限，例如 20。
- 名称只允许现有固定视图、`stock_search` 和 `stock_[0-7]`。
- 每个工件解压后仍不超过 5,000,000 字节。
- 每个对象 key 必须位于当前 release 前缀。
- SHA-256、声明字节数和 R2 custom metadata 必须一致。
- 所有工件到齐后才能切换 `active_release`。

不要把名称正则简单放宽后接受任意 manifest 工件。

### 8.2 读取端

建议增加严格白名单 API：

```text
GET /api/v2/stock-search?date=YYYY-MM-DD
GET /api/v2/stocks/stock_3?date=YYYY-MM-DD
```

也可以复用 view route 读取 `stock_search`，但个股分片应有独立的严格路由。

读取端必须：

- 从 D1 release manifest 解析 R2 key。
- 校验 key 位于对应 release 前缀。
- 校验 R2 custom metadata 的 `asOf` 和 `releaseId`。
- 直接流式返回 R2 body，不在 Worker 中 `JSON.parse` 个股分片。
- 设置 `X-Flow-As-Of`、`X-Flow-Release-Id` 和 ETag。
- 浏览器同时校验 dashboard、search、shard 三者的日期和 release ID。
- 任一不一致时保留旧的完整页面并提示“新版本仍在发布”，不得拼接两个版本。

### 8.3 历史日期

未来每个发布版本的个股工件应随 release manifest 永久定位。指定历史日期时，应读取该日期最新已发布、已通过门禁的 manifest。

旧发布版本在本功能上线前没有个股工件。遇到这种日期应返回明确的 `stock_model_not_published_for_date`，不能回退到当前日期。是否回补旧日期应作为独立任务评估。

## 9. 推荐代码改动范围

可能涉及：

- `src/trading_desk/etf_stock_flow.py`
- `src/trading_desk/read_models.py`
- `scripts/build_dashboard_snapshot.py` 或新的 sidecar 构建脚本
- `scripts/build_read_models.py`
- `scripts/publish_read_models.py`（通常只需兼容检查，不应重写）
- `.github/workflows/daily-data.yml`
- `web/app/lib/dashboard-data.ts`
- `web/app/lib/release-store.ts`
- `web/app/dashboard-bootstrap.tsx`
- `web/app/dashboard.tsx`
- 新的 `web/app/stock-detail-view.tsx`
- `web/app/globals.css`
- `web/app/api/v1/admin/read-model-import/route.ts`
- 新的只读个股搜索/分片 API route
- Python 与 Worker/渲染测试

尽量把个股页拆成独立组件，不要继续扩大已经很长的 `dashboard.tsx`。

## 10. 实施顺序

1. 增加纯函数级个股全量聚合和唯一键测试。
2. 生成 `stock_search` 与 8 个分片 sidecar；增加确定性和体积测试。
3. 将工件纳入 release hash 和 manifest；确认重复构建字节完全一致。
4. 调整上传白名单与原子激活检查。
5. 增加只流式读取的搜索、分片 API。
6. 增加统一搜索组件，分离全局搜索与 ETF 表格筛选状态。
7. 增加个股独立视图和稳定 URL。
8. 将 ETF 详情中的个股行改为可点击入口。
9. 完成单元、构建、渲染和生产 canary。
10. 最后再提交、推送、运行 Daily market data 并验证新 release；不要先切生产指针再补工件。

## 11. 自动化测试要求

### 11.1 Python 数据测试

至少覆盖：

- 公式复算正确。
- 同日 PCF 对齐。
- 次日 PCF 的 `PRE_TRADING_DAY` 对齐。
- 日期错配返回 unavailable，不回退。
- 相同数字代码、不同上市市场不合并。
- 同一个股来自多个 ETF 时正确聚合正向与负向贡献。
- 个股唯一键使用 `listing_market + stock_code`。
- 搜索索引覆盖全量聚合结果，不受全局前 500、ETF 前 12 限制。
- 分片函数确定且所有条目只进入一个分片。
- 贡献列表截断不改变完整 `netShares`。
- 名称缺失时不伪造名称。
- 每个工件字节数不超过 5,000,000。
- 相同输入两次构建得到完全相同的 release ID、hash 和字节。

### 11.2 Worker/API 测试

至少覆盖：

- manifest 只接受固定工件名。
- 超过工件数量或体积上限拒绝。
- stock shard 路径穿越和未知 shard 拒绝。
- release/asOf/metadata 不一致时 fail closed。
- 当前和指定历史日期不会串版本。
- R2 body 被流式返回；测试应防止请求路径解析完整 JSON。
- 所有工件未齐时不能激活 release。

### 11.3 前端测试

至少覆盖：

- 输入 `688126` 能优先显示代码精确匹配。
- 输入“沪硅产业”能显示名称匹配。
- ETF 和个股结果正确分组。
- 键盘和鼠标均可进入详情。
- 从 ETF 详情点击个股和从搜索进入得到相同 URL 与数据。
- 直接刷新个股 URL 能恢复页面。
- 浏览器返回键回到来源页。
- 日期或 release 不一致时不显示拼接数据。
- 无覆盖显示不可用，不显示 0。
- 少于两个历史节点不画伪折线。
- 移动端搜索结果、表格和详情不溢出。

## 12. 本地验证命令

```bash
PYTHONPATH=. PYTHONPYCACHEPREFIX=/tmp/flow-pycache .venv/bin/pytest -q
cd web
npm test
npm run lint
```

还要运行读模型构建并检查体积：

```bash
python scripts/build_read_models.py \
  --snapshot dashboard-snapshot.json \
  --output-dir read-models \
  --require-materialized
```

检查：

- manifest 工件数量和固定名称。
- 每个 JSON 的解压后字节数。
- `stock_search` 数量与所有分片去重后的数量一致。
- 每个搜索条目可以在声明分片中找到且唯一。
- ETF 贡献合计可以复算个股 `netShares`。

## 13. 生产验收清单

生产发布后，以当前 active release 的真实数据验收，不能写死示例日期：

1. 打开 `https://flow.xiaoker.com/?view=radar`。
2. 打开任一已覆盖 ETF，确认个股列表仍正常。
3. 点击某个股，确认进入稳定个股 URL。
4. 搜索同一个股代码和名称，确认进入同一 URL。
5. 核对页面 `asOf`、份额日、PCF 日和 release ID 一致。
6. 抽取至少一个贡献 ETF，用官方份额差、最小申赎单位和 PCF 数量独立复算。
7. 刷新个股页 10 次，确认没有 Error 1102、长时间加载或 Worker 资源超限。
8. 检查浏览器控制台无错误，移动端无横向溢出。
9. 检查旧 active release 只在新工件全部上传并通过 QA 后才被替换。

## 14. 完成定义

只有同时满足以下条件才算完成：

- 两条入口进入同一个个股页。
- URL 可直接访问和刷新。
- 搜索索引不是从已截断的前 500/前 12 数据反推。
- 数据口径、日期、单位、代码和名称可追溯。
- 未覆盖和历史缺失不会显示成 0。
- Worker 不做全量计算或大 JSON 解析。
- 所有新增工件有体积门禁且发布原子化。
- 现有测试与新增测试全部通过。
- 生产 canary 通过，没有 1102。
- 未改动或提交本任务范围外的 `reports/` 和 `memory/`。

## 15. 给实现者的禁止项

- 不要为了快速搜索把几千只个股逐行写进 D1，再让浏览器每次查询 D1。
- 不要在 Worker 请求期间读取 PCF 原始文件并现场聚合。
- 不要把完整个股模型继续塞进 `radar.json` 或 `bootstrap.json`。
- 不要用当前日期数据填充历史个股页。
- 不要把贡献 ETF 截断后的和当成完整个股净变化。
- 不要使用市场级两融余额冒充个股两融余额。
- 不要使用缺失价格计算亿元金额。
- 不要把估算股数称为真实资金流或实际成交。
- 不要因上线新页面而放松 QA、大小、hash、release 或来源日期门禁。
