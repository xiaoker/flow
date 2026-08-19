# Flow 免费额度核验记录（2026-08-19）

本记录只记录一次只读用量核验，不改变 Worker 的生产读取路径，不修改 D1/R2 数据，也不把当前用量直接解释为“已经收费”。它用于补充主数据完整性计划中“免费额度证据”这一项。

## 采样范围与方法

采样时间：2026-08-19（Asia/Shanghai）。

使用当前 Cloudflare 账号的 Wrangler 登录态执行以下只读检查：

```text
cd web
npx wrangler whoami
npx wrangler d1 info flow --json
npx wrangler r2 bucket list
npx wrangler r2 bucket info flow --json
```

当前 Worker 配置在 `web/wrangler.jsonc` 中声明的资源为：

- Worker：`flow`
- D1：`flow`，database id `6b85c600-8a90-4cc2-8742-c0df4392c6aa`
- R2：`flow`

## 实测结果

### D1

`wrangler d1 info flow --json` 返回的最近 24 小时计数：

| 指标 | 实测值 | 当前免费日额度 | 相对额度 |
| --- | ---: | ---: | ---: |
| 数据库存储 | 73,367,552 bytes（约 73.4 MB） | 5 GB | 约 1.4% |
| 行读取 | 7,675,626 | 5,000,000 行/日 | 约 153.5% |
| 行写入 | 381,193 | 100,000 行/日 | 约 381.2% |
| 表数量 | 13 | — | — |

这里的“相对额度”是把一次 24 小时采样与 Cloudflare 公布的免费日额度作比较，**不是账单金额，也不能单独证明账号已经切换到付费计划**。它说明当前采样窗口存在明显的免费额度超用风险；如果账号仍在 Free 计划，达到日限额的查询可能被拒绝，进而表现为采集失败或页面暂时不可用。

官方定义与限制见 [Cloudflare D1 定价](https://developers.cloudflare.com/d1/platform/pricing/)。D1 免费额度按日重置；达到 Free 计划限制后，查询可能失败，而不是自动产生一笔可推断的付费账单。

### R2

`wrangler r2 bucket info flow --json` 返回：

| 指标 | 实测值 | 当前免费月额度 | 相对额度 |
| --- | ---: | ---: | ---: |
| 对象数 | 9,795 | — | — |
| 存储 | 约 1.2 GB | 10 GB-month | 约 12% |
| 位置 | WNAM | — | — |
| 存储类型 | Standard | — | — |

R2 当前存储量低于免费存储额度；但本次没有得到完整的当月 Class A/Class B 操作计数，因此不能把 R2 的免费额度 DoD 标记为“全部通过”。对象数本身也不是操作额度。

官方定义与限制见 [Cloudflare R2 定价](https://developers.cloudflare.com/r2/pricing/)。

### Workers、GitHub Actions 与 1102

本次未取得账号级的完整月度 Workers 请求、CPU、GitHub Actions 分钟和 R2 操作账单明细，因此这些项目仍保持“未形成月度证据”。Worker 的 `1102 Worker exceeded resource limits` 不能仅凭 D1 计数归因于免费额度；它也可能由单请求 CPU、内存、子请求或响应流处理超限触发。对应限制应按 [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) 单独核验。

GitHub Actions 的仓库级 workflow 用量接口在当前授权下返回不可用，且没有为了拿账单数据而扩大 Token scope；因此不把缺少账单接口误报为“没有消耗”。

## 结论

当前严格 DoD **不能标记为免费额度通过**：

1. R2 存储本身处于较安全区间，但缺少当月操作量证据。
2. D1 最近 24 小时的行读取、行写入采样值分别高于 Free 日额度；这应视为需要立即治理的风险信号。
3. 这些数值不等于已经收费，也不等于所有流量都是页面请求；采集、重试、回填和读模型构建都可能贡献行读写。
4. 在没有按资源、按工作流、按日期拆分用量之前，不能用一次页面访问成功来证明“不会超过免费额度”。

## 后续治理要求

在主数据计划的免费额度 DoD 完成前，建议保持以下硬门槛：

- 每次采集窗口在写入前检查预计行写入量；超过预算则停止可选回填，不阻断已验证的核心数据发布。
- 将历史回填、PCF 补录、日常采集、读模型发布拆成有界任务；禁止同一批数据在重试中重复写入。
- 对 D1 的全表扫描、逐 ETF 查询和页面重复请求做查询预算；优先读取已发布分片，不在 Worker 请求中重算历史状态。
- 每日保存 D1 `d1 info`、R2 存储/操作量和 Worker 请求/CPU 的时间戳快照，至少连续 30 天后再做月度判断。
- 只有在 Cloudflare 控制台或官方 Analytics API 给出完整账单周期数据后，才把“免费额度是否会产生付费”判定为通过或失败。

本记录不改变任何生产数据。它与最终验收记录中的“免费额度尚未形成月度用量证据”保持一致。
