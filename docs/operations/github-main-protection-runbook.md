# GitHub `main` 分支保护运行手册

本文件描述仓库级治理要求。Branch protection/ruleset 是 GitHub 仓库设置，不能通过
一次代码提交自动启用；管理员必须在 GitHub 仓库设置中为 `main` 创建并启用规则。
启用前应确认规则中的 required status check 名称与 GitHub 实际显示的 check run 名称
完全一致。

## 目标规则

目标分支：`main`。

建议启用以下约束：

- 必须通过 Pull Request，至少 1 个 approving review；启用 stale review dismissal 和
  conversation resolution。
- 禁止直接 push、force push 和删除 `main`；建议对 administrators 也执行规则。
- 将源码入口门禁设为 required：`Candidate source gate (required source lanes)`。
- 将稳定的 CI 检查设为 required：`Test Suite (Python 3.10)`、
  `Test Suite (Python 3.12)`、`Specialist regression suites`、
  `Semantic migration regression (exact SHA)`、
  `MariaDB 5.5 compatibility rehearsal` 和
  `Python 3.6 compatibility (VNext minimum behavior)`。
- 对 workflow 文件启用同样的 Pull Request 审查；如组织已有签名提交策略，继续要求
  signed commits/tags。

`Trusted Validation Candidate Build`、`Trusted Production Candidate Build`、浏览器、
性能和 `Production READY` 属于手动或受保护证据链，不应因为普通 push/PR 没有运行就被
误设为普通分支的必需 check。若发布流程要求它们成为发布前门禁，应在独立的 release
ruleset 或受保护 environment 中要求，而不是用一个可能长期为 skipped 的 check 保护所有
普通合并。

## 启用后核验

管理员应从 GitHub API 或 Branch protection 页面确认：`main` 已受保护、required status
checks 已开启、force-push/deletion 已禁止，且 Pull Request review 数为至少 1。随后用一个
无审批的测试 PR 验证它不能合并；用一个满足全部 required checks 的测试 PR 验证正常路径。

若规则发生变化，应把规则页面或 API 响应作为仓库外部治理证据保存到对应 release evidence
中。仓库内的 workflow/test 断言只能防止门禁名称和工作流契约被代码变更悄悄移除，不能替代
GitHub 仓库设置本身。
