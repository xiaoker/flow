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

命令成功才表示当前分支包含最新生产基线。分支新鲜度检查是 PR 的必需门禁；如果
`main` 在检查后继续前进，PR 必须再次同步。合并后确认远端 `main` 和部署工作流
使用同一个合并提交 SHA。

## 发布后

合并后等待 `deploy-worker.yml` 完成，并检查生产 canary。若部署失败，先保留 PR、
工作流日志和发布证据，再修复；不要在旧分支上继续堆叠新功能。
