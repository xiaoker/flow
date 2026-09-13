# 分支同步与发布约束

本仓库的 `main` 是唯一生产基线。所有实现分支都必须从最新的
`origin/main` 创建，并通过 Pull Request 合并；不直接向 `main` 推送。

## 开始一个任务

```bash
git fetch origin main
git switch -c codex/<task-name> origin/main
```

创建分支后立即推送一次，使远端、PR 和本地都有同一个起点：

```bash
git push -u origin codex/<task-name>
```

## 开发期间

- 每个可回滚的逻辑节点都提交并推送；不要等全部工作结束才第一次推送。
- PR 的 base 固定为 `main`，并在描述中记录验证命令和数据/迁移影响。
- `reports/`、`memory/` 等本地运行产物不是发布输入；提交前使用
  `git status --short` 逐项确认，只 `git add` 已确认的路径，禁止盲目使用
  `git add -A`。
- `main` 有新提交后，先同步再继续开发。未共享的分支可以 rebase；已经推送或
  有人在使用的分支使用普通 merge，避免改写远端历史：

```bash
git fetch origin main
git merge --no-edit origin/main
git push
```

## 合并前检查

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
```

命令成功才表示当前分支包含最新生产基线。分支新鲜度检查是发布前门禁；如果
`main` 在检查后继续前进，PR 必须再次同步。合并后确认远端 `main` 和部署工作流
使用同一个合并提交 SHA。

> 限制：当前仓库是 GitHub 私有仓库的免费计划，不能启用原生 required status
> checks/branch protection。因此 workflow 会明确失败并提示同步，但平台不会替
> 管理员强制阻止手动合并。没有完成新鲜度检查时不得合并；不要使用管理员绕过。

## 发布后

合并后等待 `deploy-worker.yml` 完成，并检查生产 canary。生产部署工作流由
`pull_request` 的 `closed` 事件触发，并且只在 `merged == true` 时执行；它使用
PR 的 `merge_commit_sha` 检出代码。这样即使一次合并请求返回不明确、先在分支上
生成了 squash commit，随后落到 `main` 的合并提交为空，也仍会按 PR 的真实差异
触发部署，不会被 `push.paths` 静默跳过。`workflow_dispatch` 仅用于明确的人工
维护动作。

若合并 API 或 CLI 返回超时、GraphQL 错误或其他不明确结果，禁止立即重试。先执行
`git fetch origin --prune`，核对 PR 的 `state`、`mergedAt`、`mergeCommit`、
`headRefOid`，并比较 `origin/main` 与 PR head 的树和补丁：若 PR 内容已经进入
`main`，只等待对应的 `Deploy Flow Worker`/canary；若仍未合并，才允许重新提交一次
合并请求。这个核对步骤是幂等边界，不能用第二个空合并提交代替。

当前账号还存在 Cloudflare Workers Builds 的原生 Git 集成。它是独立于 GitHub
Actions 的第二条部署通道；在生产切换或影子运行前，必须在 Cloudflare Worker
设置中将其改为仅上传版本（例如 `npx wrangler versions upload`）或断开自动部署，
并保留截图/审计证据。仓库代码不能隐式修改该账号级设置。若两条通道尚未收敛，
不要把 Cloudflare Builds 的成功误报为 GitHub 部署工作流成功。

若部署失败，先保留 PR、工作流日志和发布证据，再修复；不要在旧分支上继续堆叠新功能。
