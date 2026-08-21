# Gate A～F 外部环境证据运行手册

本手册只描述必须在 Candidate、MariaDB 兼容主机或生产环境执行的证据。仓库内的 SQLite/fixture、合成浏览器 benchmark 和旧 checkout 结果不能替代这些证据。所有命令都必须从待验收的 exact commit checkout 执行：

```bash
git fetch --tags --prune origin
git checkout --detach <candidate-sha>
test -z "$(git status --porcelain)"
git rev-parse HEAD
```

## 证据文件统一要求

`scripts/diagnostics/gate_matrix.py` 从以下环境变量读取外部证据：

```text
COVERAGE_GATE_A_BACKUP_EVIDENCE
COVERAGE_GATE_A_MARIADB_EVIDENCE
COVERAGE_GATE_B_DB_EVIDENCE
COVERAGE_GATE_C_RESTART_EVIDENCE
COVERAGE_GATE_D_CORPUS_EVIDENCE
COVERAGE_GATE_E_BROWSER_EVIDENCE
COVERAGE_GATE_E_PERF_EVIDENCE
COVERAGE_GATE_F_INVENTORY_EVIDENCE
COVERAGE_GATE_F_CUTOVER_EVIDENCE
COVERAGE_GATE_F_ACCEPTANCE_EVIDENCE
```

优先使用 `Evidence Manifest v2`。每个 manifest 必须声明正确的 `gate-a`～`gate-f`，并绑定当前 `candidate_revision` 与 `release_identity.commit_sha`。每条 PASS 记录必须带：

- 非空 `host_identity`、`command_or_action`、`started_at`、`finished_at`；
- 整数 `exit_code=0`，且 `synthetic=false`；
- 可读取的 `artifact_path` 和匹配的 SHA256；
- 数据库类证据的 `database_runtime_identity`；
- 参与计算的 source artifact SHA256。

Gate matrix 会拒绝把某个 Gate 的 manifest 复用给另一个 Gate，也会拒绝旧 commit、手写 `status=PASSED` 或 hash 不匹配的 artifact。

## Gate A：真实备份恢复迁移

先在 Current/生产环境完成 freeze/drain，生成完整 dump，并把 dump、`.sha256`、schema/语义快照放在 Current/Candidate 部署根之外。目标数据库必须是新建的 disposable database，不能使用 Current、Candidate 或任何已有业务库。

在有 MariaDB 5.5 服务器和 `mariadb`/`mysql` 客户端的 rehearsal 主机执行：

```bash
python3 scripts/upgrade/run_verified_backup_rehearsal.py \
  --repo-root "$PWD" \
  --config /secure/coverage-candidate-mysql.json \
  --backup /secure/backups/coverage-full.sql.gz \
  --backup-sha256 "$(awk '{print $1}' /secure/backups/coverage-full.sql.gz.sha256)" \
  --require-version-prefix 5.5 \
  --deployment-root /srv/fos-coverage/current \
  --deployment-root /srv/fos-coverage/candidate \
  --output /secure/evidence/gate-a/verified-backup-restore.json \
  --manifest-output /secure/evidence/gate-a/evidence-manifest-v2.json \
  --create-disposable
```

该命令会：

1. 验证 dump 的 SHA256 和 gzip framing；
2. 拒绝位于部署树内的 backup；
3. 检查 source/target disposable 数据库在执行前不存在；
4. 将 dump 恢复到 Legacy source，再迁移到独立 Empty VNext target；
5. 检查 MariaDB runtime identity、semantic hash、重跑幂等和目标表清单；
6. 只删除本次创建的两个数据库。

`scripts/diagnostics/mysql_vnext_integration.py --create-disposable --migration-rehearsal` 仍可用于 MariaDB SQL/事务/fixture 回归，但输出明确标记 `synthetic=true`，不能填充 `COVERAGE_GATE_A_BACKUP_EVIDENCE`。需要同时验证 Gate C durable import 时追加 `--scan-import-rehearsal`；该项会覆盖 busy zero-residue、staged artifact recovery、fencing CAS、CURRENT 原子发布和重复恢复幂等，但同样只属于本地 synthetic rehearsal。

## Gate B～D：目标库、重启和解析器证据

这些证据必须包含执行主机、数据库 runtime fingerprint 或 parser binary/version/SHA，并引用本次 exact commit：

- Gate B：在真实 Candidate target 上运行 Analysis Domain backfill、orphan 检查、semantic hash reconciliation 和重跑幂等；
- Gate C：中断 import、重启 worker/API、验证 fencing/checkpoint/read-set 和 current pointer 不回退；
- Gate D：使用目标主机实际 parser/toolchain 跑完整 deterministic corpus，保留 parser 版本、helper SHA、命令和零 false-positive 结果。

仓库内的 `parser_toolchain_preflight` 在没有真实 helper 时会保持 `INCOMPLETE`，不能通过设置环境变量把 builtin parser 伪装成生产 parser。

## Gate E：浏览器功能与跨层性能分开取证

浏览器功能证据必须来自真实 HTTP + Chromium，并保存 route/network/console/report artifact。性能证据必须另外保存 DB query/row 计数、Sidecar decode 计数、expand p95、峰值 RSS、100k virtual-scroll resident lines 和环境身份。只有浏览器功能绿而缺少跨层指标时，Gate E 仍是 `INCOMPLETE`；不得用 `--allow-partial` 结果作为 release performance PASS。

## Gate F：切换、回滚和 48 小时窗口

正式切换前重新生成 fresh inventory，至少覆盖 process/service、release identity、Current/Candidate roots、DB fingerprint、schema/table counts、jobs、磁盘公式、Nginx/auth boundary 和 backup location。再执行 freeze → final backup → Candidate rehearsal → traffic-closed verification → cutover → forced rollback rehearsal，并保留完整 before/target/rollback release identity。

Acceptance window 必须在窗口结束后运行：

```bash
python3 scripts/diagnostics/acceptance_window_audit.py \
  --input /secure/evidence/gate-f/acceptance-window-input.json \
  --output /secure/evidence/gate-f/acceptance_window_checks.json
python3 scripts/diagnostics/skill_drift_audit.py \
  --input /secure/evidence/gate-f/skill-drift-input.json \
  --candidate-revision "$(git rev-parse HEAD)" \
  --output /secure/evidence/gate-f/skill_drift_audit.json
```

## 组装和验收

在设置上述环境变量并将每个 manifest 放入其声明的 Gate 目录后执行：

```bash
python3 scripts/diagnostics/gate_matrix.py \
  --repo-root "$PWD" \
  --output /secure/evidence/gate-matrix.json
```

只要任一外部 artifact 缺失、Gate 不匹配、commit 不匹配、runtime identity 不完整、exit code 非零或证据为 synthetic，命令就必须保持非零并输出 `INCOMPLETE`/`BLOCKED`。在真实外部证据产生前，不得将本地 Gate Matrix 的 `--allow-incomplete` 结果写成已发布或已验收。
