# Gate A～F 外部环境证据运行手册

本手册只描述必须在 Candidate、MariaDB 兼容主机或生产环境执行的证据。仓库内的 SQLite/fixture、合成浏览器 benchmark 和旧 checkout 结果不能替代这些证据。所有命令都必须从待验收的 exact commit checkout 执行：

```bash
git fetch --tags --prune origin
git checkout --detach <candidate-sha>
test -z "$(git status --porcelain)"
git rev-parse HEAD
```

## CI 状态语义

普通 push/PR 的 `Candidate source gate (required source lanes)` 只表示源码候选
检查通过，不代表 Production READY。真实 Candidate 浏览器、生产备份恢复和
`release_eligible` 跨层性能证据只由手动 `workflow_dispatch` 的
`Production READY gate (manual external evidence)` 汇总；其中任一 lane 未运行或
失败，Production READY 都不成立。

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

`build_gate_evidence.py` 会把已提供的 Gate A MariaDB、Gate B backfill、Gate C
restart、Gate D corpus、Gate E browser/performance 和 Gate F cutover/acceptance
JSON 复制到对应 bundle 文件，并在 Evidence Manifest v2 中记录外部输入 SHA256。
缺失或不合法的输入仍保持 `INCOMPLETE`，不会被复制动作提升为 PASS。

优先使用 `Evidence Manifest v2`。每个 manifest 必须声明正确的 `gate-a`～`gate-f`，并绑定当前 `candidate_revision` 与 `release_identity.commit_sha`。每条 PASS 记录必须带：

- 非空 `host_identity`、`command_or_action`、`started_at`、`finished_at`；
- 整数 `exit_code=0`，且 `synthetic=false`；
- 可读取的 `artifact_path` 和匹配的 SHA256；
- 数据库类证据的 `database_runtime_identity`；
- 参与计算的 source artifact SHA256。

Gate matrix 会拒绝把某个 Gate 的 manifest 复用给另一个 Gate，也会拒绝旧 commit、手写 `status=PASSED` 或 hash 不匹配的 artifact。对 Gate A 的 MariaDB rehearsal，运行时指纹还必须包含以 `5.5` 开头的数据库版本；Gate A backup、Gate B backfill 和 Gate C restart 的 flat JSON 证据同样必须显式携带非空 `database_runtime_identity`，不能靠通用 `evidence_class` 名称绕过校验。

## Gate A：真实备份恢复迁移

先在 Current/生产环境完成 freeze/drain，生成完整 dump，并把 dump、`.sha256`、schema/语义快照放在 Current/Candidate 部署根之外。目标数据库必须是新建的 disposable database，不能使用 Current、Candidate 或任何已有业务库。

在有 MariaDB 5.5 服务器和 `mariadb`/`mysql` 客户端的 rehearsal 主机执行：

```bash
python3 scripts/upgrade/run_verified_backup_rehearsal.py \
  --repo-root "$PWD" \
  --config /secure/coverage-candidate-mysql.json \
  --backup /secure/backups/coverage-full.sql.gz \
  --backup-sha256 "$(awk '{print $1}' /secure/backups/coverage-full.sql.gz.sha256)" \
  --backup-manifest /secure/backups/backup-manifest.json \
  --require-version-prefix 5.5 \
  --deployment-root /srv/fos-coverage/current \
  --deployment-root /srv/fos-coverage/candidate \
  --output /secure/evidence/gate-a/verified-backup-restore.json \
  --manifest-output /secure/evidence/gate-a/evidence-manifest-v2.json \
  --create-disposable
```

该命令会：

1. 验证 dump 的 SHA256 和 gzip framing；
2. 验证独立 `backup-manifest.json`：`production_backup`、`synthetic=false`、
   外置 backup root、restore smoke、源库快照，以及 production/operator/时间戳
   attestation；manifest 中的 dump SHA 和大小必须与输入一致；
3. 拒绝位于部署树内的 backup 或 provenance manifest；
4. 检查 source/target disposable 数据库在执行前不存在；
5. 将 dump 恢复到 Legacy source，再迁移到独立 Empty VNext target；
6. 检查 MariaDB runtime identity、semantic hash、重跑幂等和目标表清单；
7. 只删除本次创建的两个数据库。

`backup-manifest.json` 必须来自真实 backup workflow，并包含如下来源证明：

生成生产 backup 时，应显式提供来源环境和操作者身份，例如：

```bash
export COVERAGE_BACKUP_SOURCE_ENVIRONMENT=production
export COVERAGE_BACKUP_OPERATOR="$USER"
```

backup workflow 会把这两个值和 attestation 时间写入 manifest；未设置时保持空值，
后续 rehearsal 会 fail closed。

```json
{
  "status": "BACKUP_VERIFIED",
  "evidence_class": "production_backup",
  "synthetic": false,
  "backup_root_external": true,
  "provenance": {
    "source_environment": "production",
    "operator": "operator-id",
    "attested_at": "2026-08-21T00:00:00Z"
  }
}
```

缺少 manifest、使用 mock/synthetic manifest、restore smoke 未通过或只提供
手写 `status=PASSED` rehearsal JSON，都会保持 `INCOMPLETE`，不能填充
`COVERAGE_GATE_A_BACKUP_EVIDENCE`。

`scripts/diagnostics/mysql_vnext_integration.py --create-disposable --migration-rehearsal` 仍可用于 MariaDB SQL/事务/fixture 回归，但输出明确标记 `synthetic=true`，不能填充 `COVERAGE_GATE_A_BACKUP_EVIDENCE`。需要同时验证 Gate C durable import 时追加 `--scan-import-rehearsal`；该项会覆盖 busy zero-residue、staged artifact recovery、fencing CAS、CURRENT 原子发布和重复恢复幂等，但同样只属于本地 synthetic rehearsal。

## Gate B～D：目标库、重启和解析器证据

这些证据必须包含执行主机、数据库 runtime fingerprint 或 parser binary/version/SHA，并引用本次 exact commit：

- Gate B：在真实 Candidate target 上运行 Analysis Domain backfill、orphan 检查、semantic hash reconciliation 和重跑幂等；
- Gate C：中断 import、重启 worker/API、验证 fencing/checkpoint/read-set 和 current pointer 不回退；
- Gate D：使用目标主机实际 parser/toolchain 跑完整 deterministic corpus，保留 parser 版本、helper SHA、命令和零 false-positive 结果。

仓库内的 `parser_toolchain_preflight` 在没有真实 helper 时会保持 `INCOMPLETE`，不能通过设置环境变量把 builtin parser 伪装成生产 parser。

如果 Candidate runtime 通过配置文件选择 parser，应先对同一份配置执行：

```bash
python3 scripts/diagnostics/parser_toolchain_preflight.py \
  --config /secure/coverage-candidate.json \
  --require-external
```

Gate Matrix 也应绑定这份配置，确保审计对象就是实际 runtime 选择的 adapter：

```bash
python3 scripts/diagnostics/gate_matrix.py \
  --runtime-config /secure/coverage-candidate.json \
  --allow-incomplete \
  --output /secure/evidence/gate-matrix.json
```

目标主机上的 parser 应通过同一版本化 JSON adapter 执行 corpus；例如：

```bash
python3 scripts/diagnostics/deterministic_inheritance_corpus.py \
  --fixture tests/fixtures/inheritance_deterministic_corpus.json \
  --adapter json-cli-v1 \
  --command '/opt/coverage/bin/coverage-cpp-parser' \
  --require-external \
  --output /secure/evidence/gate-d/deterministic-corpus-run.json
```

该命令会先验证 executable、版本、binary SHA 和 `coverage-cpp-parser-v1`
协议 smoke test，再用外部 adapter 执行与本地相同的 decision corpus。输出本身
仍是 parser-run artifact（`release_eligible=false`），必须由取证流程再包装为带
exact candidate revision、主机身份、命令、时间戳和 artifact SHA 的 Gate D evidence；
helper 失败时不得回退到 builtin parser。

## Gate E：浏览器功能与跨层性能分开取证

浏览器功能证据必须来自真实 HTTP + Chromium，并保存 route/network/console/report artifact。性能证据必须另外保存 DB query/row 计数、Sidecar decode 计数、expand p95、峰值 RSS、100k virtual-scroll resident lines 和环境身份。只有浏览器功能绿而缺少跨层指标时，Gate E 仍是 `INCOMPLETE`；不得用 `--allow-partial` 结果作为 release performance PASS。
Gate Matrix 还要求跨层性能 artifact 明确声明 `synthetic=false` 且
`release_eligible=true`；即使指标齐全，缺少发布资格声明也不能推进 Gate E。

仓库内的 `real_browser_evidence.js` 提供统一采集入口。它会先访问 Candidate 的
`/api/coverage/release` 并 exact 比较 commit，再打开真实 HTML 页面执行 100k
virtual-scroll sweep；release identity 不匹配、HTTP/Chromium 失败或跨层指标缺失
都会 fail closed。浏览器功能和跨层性能使用不同的 evidence envelope：

```bash
npm ci  # 取证主机按既有方式准备 Python/Node 依赖
node scripts/diagnostics/real_browser_evidence.js \
  --url 'https://<candidate-host>/<real-report-file>.gcov.html' \
  --expected-revision "$(git rev-parse HEAD)" \
  --release-validation-session-id "$RELEASE_VALIDATION_SESSION_ID" \
  --candidate-artifact-sha256 "$CANDIDATE_ARTIFACT_SHA256" \
  --served-root-sha256 "$SERVED_ROOT_SHA256" \
  --header "X-Remote-User=$COVERAGE_BROWSER_USER" \
  --header "X-Remote-Role=reviewer" \
  --output /secure/evidence/gate-e/real-browser-workload.json \
  --browser-evidence-output /secure/evidence/gate-e/browser-evidence.json \
  --evidence-output /secure/evidence/gate-e/performance-evidence.json
```

将 `COVERAGE_GATE_E_BROWSER_EVIDENCE` 指向 `browser-evidence.json`，将
`COVERAGE_GATE_E_PERF_EVIDENCE` 指向 `performance-evidence.json`。采集器不会把
header value 写入 artifact；但仍应通过环境变量或受控 secret 注入凭据，不要把
真实 token 直接提交到 shell history。该命令不执行分析保存、确认、导出或其他
mutation；需要 mutation rehearsal 时应使用独立 Candidate 数据库和专门的 API
回归脚本。

发布性能 A/B 也必须绑定同一个 attempt 和已发布 artifact：

```bash
node scripts/diagnostics/release_performance_ab.js \
  --baseline-artifact /secure/evidence/gate-e/baseline.json \
  --candidate-artifact /secure/evidence/gate-e/candidate.json \
  --baseline-commit "$PREVIOUS_COMMIT_SHA" \
  --candidate-commit "$CANDIDATE_COMMIT_SHA" \
  --workload-hash "$RELEASE_WORKLOAD_HASH" \
  --release-validation-session-id "$RELEASE_VALIDATION_SESSION_ID" \
  --candidate-artifact-sha256 "$CANDIDATE_ARTIFACT_SHA256" \
  --served-root-sha256 "$SERVED_ROOT_SHA256" \
  --output "/secure/evidence/gate-e/release-performance-ab-${RELEASE_VALIDATION_SESSION_ID}.json"
```

同一采集器也接入 `.github/workflows/ci.yml` 的手动
`real-browser-candidate` lane。触发 `workflow_dispatch` 时提供
`real_browser_url`（可选提供 `real_browser_expected_revision`），并在仓库/环境
Secret 中配置 `COVERAGE_BROWSER_USER`；job 会上传三个 exact-SHA JSON artifact。
未提供 Candidate URL 时该 lane 不运行，普通 CI 仍只保留明确标记为 synthetic 的
fixture/browser evidence。

CI 的 `cross-layer-performance` job 不再接收或读取一个来自其他主机的本地路径。
`real-browser-candidate` 会先把本次 run/attempt 的完整 evidence package 上传为
不可变 artifact，并输出 `performance-evidence.json` 的 SHA256；下游 performance
job 按同一个 run/attempt 下载该 artifact，先校验 SHA256，再对下载后的原始 evidence
执行 `--require-cross-layer --require-release-eligible` 审计。这样 Hosted Runner
之间传递的是本次 Candidate 实际产生的 evidence，而不是 checkout 内或操作员临时
准备的同名文件。

`run_upgrade.py` 对这两类证据也采用同一口径：`npm run test:browser` 只写入
`browser_fixture_regression`，不能写入生产 Browser Gate。正式升级必须在配置中
提供 `candidate_browser_url` 和外部 `candidate_browser_evidence_path`；该文件必须
来自 `real_browser_evidence.js --browser-evidence-output`，并绑定 exact commit、
served release identity、真实 HTTP、Chromium、artifact SHA、当前
`release_validation_session_id`、Candidate artifact SHA、Served Root SHA 和
`synthetic=false`，否则 Final Gate 保持 `NOT_READY`。三类外部 evidence 的路径都必须
按 attempt 隔离；即使两个 attempt 使用同一 Candidate SHA，也不能复用旧 JSON。

## 首次接管：建立 immutable previous baseline

尚未建立合法 `publish_root/CURRENT` 的旧环境必须先执行一次显式 bootstrap；正常
`run_upgrade.py` 不会在缺少 `CURRENT` 时偷偷创建基线。先从实际 Served Root
取得完整 release identity JSON（不是仓库 checkout 的猜测值），再执行：

如果实机是旧的 Flat Root（HTML、JS/CSS 直接位于同一个根目录，且没有
`reports/`、`assets/`、`registry/` 和 `.source_cache/`），不能直接把它交给
bootstrap。先用专用的 Legacy Flat Adoption 生成独立 staging；工具只复制原始文件，
将非 HTML 文件同时保存到 `reports/` 和 `assets/`，并为每个 HTML 在 `<head>` 中添加
`coverage-project=FOS_V6R2`、`coverage-report-mode=LEGACY_STATIC` 和基于“原始相对路径
+ 原始 HTML SHA256”的确定性 `coverage-report-id`。registry 只记录项目、Legacy
模式、`reports` 根目录和原始文件指纹，不生成 scan、repository、file、Sidecar 或
asset identity：

```bash
python3 scripts/release/prepare_legacy_flat_adoption.py \
  --served-root /export0810/onesensor \
  --output-root /secure/staging/legacy-flat-e9fcc837 \
  --release-identity /secure/evidence/e9fcc837-release-identity.json \
  --expected-commit-sha e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc
```

该命令不会修改原始 Flat Root；后续 bootstrap 的 `--served-root` 和
`--served-identity` 都必须指向它生成的 staging。只有已经具备完整
`reports/`、`assets/`、`registry/` 的 immutable-shaped Served Root 才能跳过该阶段。
下面的 bootstrap 命令以 Flat adoption staging 为例；已是 immutable-shaped 的环境应
改用其实际 Served Root 和对应的已核验 identity evidence。

```bash
python3 scripts/release/bootstrap_previous_release.py \
  --served-root /secure/staging/legacy-flat-e9fcc837 \
  --publish-root /srv/fos-coverage/published \
  --release-identity /secure/evidence/e9fcc837-release-identity.json \
  --served-identity /secure/staging/legacy-flat-e9fcc837/release_identity.json \
  --session-id previous-e9fcc837 \
  --switch
```

工具会读取并核对 Served Root、重新计算 reports/assets/registry 及完整文件清单，
生成并验证 immutable previous release，然后原子创建 `CURRENT`。已有 `CURRENT`、
identity 缺失/不匹配或 artifact hash 失败都会拒绝操作；bootstrap 不属于普通升级
路径。Legacy adoption 后的 `validate_current()` 必须先通过；随后 Production Candidate
Builder 才能从这个 CURRENT 读取目标 binding。Builder 会刷新目标 release asset
manifest 中声明的根级/模板资源，但不会把 `reports/**/*.html` 当成同名静态 asset alias
覆盖，因此 Legacy HTML 的业务正文和身份字段保持不变。

同一 Candidate 重试时不要复用旧的 validation session。`run_upgrade.py` 默认生成
`candidate-<commit-sha>-<attempt-uuid>`，并将 validation session 与 teardown evidence
写入按 `{attempt_id}` 分隔的路径；旧 attempt 即使已 rollback/teardown，也不会阻塞
同一 SHA 的新 attempt。只有在审计明确要求时才使用配置中的
`release_validation_session_id` 固定该次尝试身份。

Rollback rehearsal 也必须写入同一个 attempt namespace，并绑定本次发布的两个
artifact hash：

```bash
python3 scripts/upgrade/run_rollback_rehearsal.py \
  --output "/secure/evidence/gate-f/rollback-${RELEASE_VALIDATION_SESSION_ID}.json" \
  --revision "$CANDIDATE_COMMIT_SHA" \
  --config /srv/fos-coverage/candidate/config/coverage_config.staging.example.json \
  --release-validation-session-id "$RELEASE_VALIDATION_SESSION_ID" \
  --candidate-artifact-sha256 "$CANDIDATE_ARTIFACT_SHA256" \
  --served-root-sha256 "$SERVED_ROOT_SHA256"
```

`target_release_id`、`release_validation_session_id`、Candidate artifact SHA 和
Served Root SHA 必须全部与当前 attempt 一致；否则 rollback evidence 不能进入
Final Gate。

Candidate 必须先按用途分成两个互斥对象。受保护的
`trusted-candidate-builder.yml` 和 `build_candidate_artifact.py` 生成的是
`validation_candidate_root`：它包含确定性的浏览器/100k 性能夹具，manifest 必须是
`artifact_role=validation_fixture`、`production_publishable=false`。它可以用于浏览器、
性能、功能和生命周期验证，但永远不能传给 Publisher，也永远不能成为 `CURRENT`。

正式发布使用独立的 `production_candidate_root`。它必须由真实的
`FOS_V6R2` Served Root 构建，包含真实 `reports/`、`registry/`、Sidecar 和静态页面，
再用目标 clean checkout 的 release identity `asset_manifest` 中声明的完整资产契约刷新
所有静态资源（包括 root/web 兼容副本和目标版本新增的文件）。构建器只接受
`publish_root/CURRENT` 这个不可变指针，并把 previous release SHA、CURRENT tree hash
和 CURRENT identity 一起写入 provenance；构建、normalize 和 manifest 必须在发布前完成；Publisher
只复制并验证最终字节，不再把验证夹具升级成生产页面：

配置必须同时维护两套不可互换的 Builder policy：
`validation_candidate_builder_workflow_*` 只对应验证夹具，
`production_candidate_builder_workflow_*` 只对应生产构建器，并且每个 SHA 必须固定到
包含相应 workflow 的不可变 Git commit。生产升级只读取后者。

受保护的 `trusted-production-candidate-build` Environment 必须配置一个固定的
Environment Variable `PRODUCTION_PUBLISH_ROOT`，其值是
`coverage-production-builder` runner 上唯一权威的 immutable publication root 绝对路径。
`trusted-production-candidate-builder.yml` 只从 `${{ vars.PRODUCTION_PUBLISH_ROOT }}`
读取该值；`workflow_dispatch` 和 reusable-workflow caller 都不再接受
`publish_root`/`production_publish_root` 路径输入。变量缺失时构建器必须立即失败。
本地 observation-only 的 `production_current_binding.py --publish-root` 仍可接受路径，
但该 CLI 输入不属于受保护的正式生产构建路径。

```bash
python3 scripts/release/build_production_candidate_artifact.py \
  --publish-root /srv/fos-coverage/publish-root \
  --source-repo-root /srv/fos-coverage/source-checkout \
  --production-candidate-root /srv/fos-coverage/production-candidate \
  --release-identity-output /secure/evidence/production-release-identity.json \
  --build-workflow-identity "$PRODUCTION_BUILD_WORKFLOW_IDENTITY" \
  --build-workflow-run-id "$GITHUB_RUN_ID" \
  --build-workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --build-workflow-sha "$PRODUCTION_BUILD_WORKFLOW_SHA" \
  --expected-previous-release-sha "$CURRENT_PREVIOUS_RELEASE_SHA" \
  --expected-served-root-tree-sha256 "$CURRENT_SERVED_ROOT_TREE_SHA256" \
  --expected-current-identity-sha256 "$CURRENT_IDENTITY_SHA256"
```

这里的 `--publish-root` 必须是权威 immutable publication root；构建器自行解析
`publish_root/CURRENT`，不接受人工挑选的旧 release 目录。构建前应使用
`scripts/diagnostics/production_current_binding.py` 从同一个权威 CURRENT 读取并
冻结三个 expected binding 值；它们在构建开始时和复制完成后都会重检。CURRENT
必须有完整且通过 `validate_release_manifest()` 的 `release_manifest.json`，不会再
降级读取 `release_identity.json`。命令输出的 manifest 必须明确为
`artifact_role=production_release`、`production_publishable=true`、
`project_name=FOS_V6R2`。之后在受保护 Build job 中为这个 production manifest
生成 GitHub Actions attestation 和 detached receipt：

```bash
python3 scripts/release/sign_candidate_build_receipt.py \
  --candidate-root /srv/fos-coverage/production-candidate \
  --release-identity /secure/evidence/production-release-identity.json \
  --source-repo-root /srv/fos-coverage/source-checkout \
  --build-workflow-identity "$PRODUCTION_BUILD_WORKFLOW_IDENTITY" \
  --build-workflow-run-id "$GITHUB_RUN_ID" \
  --build-workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --build-workflow-sha "$PRODUCTION_BUILD_WORKFLOW_SHA" \
  --attestation-bundle /secure/evidence/production-candidate-attestation.bundle.json
```

Publisher 和 `run_upgrade.py` 只接受 `production_candidate_root`，并重新验证 clean
source checkout、Candidate manifest、生产项目内容、attestation、receipt 和完整
文件清单；任何 `validation_fixture`、`Coverage Candidate`、缺少 source/build
provenance 或 Served Root binding 的输入都硬失败。Validation Candidate 的
attestation 强制 `--deny-self-hosted-runners`；Production Candidate 使用受保护的
`coverage-production-builder` self-hosted lane，但仍强制 exact signer workflow SHA、
run ID/attempt、HMAC receipt、GitHub attestation 和 source SHA。`served-root-bootstrap` 只能由
`bootstrap_previous_release.py` 的专用 API 使用。

发布后的静态服务也必须遵守同一条路径契约：配置中的
`served_root_path` 必须是字面量 `publish_root/CURRENT/reports`，不能指向旧的
`/home/.../onesensor` 或其他脱离 Publisher 的目录。Final Gate 会对真实
`/coverage/` HTTP 响应及其同源静态资源逐字节核对当前 `CURRENT` release；因此
Publisher 验证的 release 和浏览器实际访问的 Served Root 必须是同一个对象。

## Gate F：切换、回滚和 48 小时窗口

正式切换前重新生成 fresh inventory，至少覆盖 process/service、release identity、Current/Candidate roots、DB fingerprint、schema/table counts、jobs、磁盘公式、Nginx/auth boundary 和 backup location。再执行 freeze → final backup → Candidate rehearsal → traffic-closed verification → cutover → forced rollback rehearsal，并保留完整 before/target/rollback release identity。

使用仓库内的 observation-only inventory 工具时，Current/Candidate 配置、服务、进程、持久化目录、外置 backup 根和代理配置都必须显式提供；缺少任一项命令都会非零退出并保持 `INCOMPLETE`。`--config` 仍是 `--candidate-config` 的兼容别名：

```bash
python3 scripts/diagnostics/production_inventory.py \
  --current-root /srv/fos-coverage/current \
  --candidate-root /srv/fos-coverage/candidate \
  --current-config /srv/fos-coverage/current/coverage_config.json \
  --candidate-config /srv/fos-coverage/candidate/config/coverage_config.staging.example.json \
  --current-repository-root /srv/fos-coverage/current \
  --candidate-repository-root /srv/fos-coverage/candidate \
  --service fos-coverage-current \
  --service fos-coverage-candidate \
  --process-pattern enhance_coverage \
  --process-pattern coverage \
  --persistent-root /srv/fos-coverage/current/.runtime-state \
  --persistent-root /srv/fos-coverage/candidate/.runtime-state-staging \
  --jobs-root /srv/fos-coverage/current/.runtime-state/jobs \
  --jobs-root /srv/fos-coverage/candidate/.runtime-state-staging/jobs \
  --backup-root /secure/backups/fos-coverage \
  --proxy-config /etc/nginx/sites-enabled/fos-coverage.conf \
  --current-release-bytes <current_release_bytes> \
  --candidate-release-bytes <candidate_release_bytes> \
  --final-target-db-estimate <final_target_db_estimate> \
  --verified-backup-bytes <verified_backup_bytes> \
  --max-temp-worktree-bytes <max_temp_worktree_bytes> \
  --migration-temp-bytes <migration_temp_bytes> \
  --output /secure/evidence/gate-f/fresh_inventory/summary.json \
  --manifest-output /secure/evidence/gate-f/evidence-manifest-v2.json
```

该工具会读取两个 DB 的 runtime fingerprint、schema/table counts、`data_version` 和 job state，并检查配置端口是否实际监听、服务 `MainPID` 是否能与进程命令行对应、backup 根是否在两个部署根之外，以及代理是否显式设置 Candidate 配置中的认证用户 header。它不会创建数据库、启动服务、修改配置或删除文件；`--output` 保存原始盘点结果，`--manifest-output` 生成可直接交给 `COVERAGE_GATE_F_INVENTORY_EVIDENCE` 的 Gate F Evidence Manifest v2。即使盘点不完整，manifest 也会保留 `INCOMPLETE`，不会伪造 PASS。

在 traffic-closed verification 前，先在最终 exact checkout 执行仓库内 source/security review：

```bash
python3 scripts/diagnostics/final_source_review.py \
  --output /secure/evidence/gate-f/final_source_review.json
python3 scripts/diagnostics/final_security_review.py \
  --output /secure/evidence/gate-f/final_security_review.json
```

这两个结果只证明 exact SHA 的 source/canonical/runtime 与静态 trust-boundary 检查；它们不能替代后续的生产进程、Target DB、反向代理和 traffic-closed read-only 证据。

随后必须针对实际运行中的 Candidate 进程执行 active-runtime audit，并显式绑定同一个
SHA；该审计会把 HTTP release endpoint 的 `commit_sha` 与候选 SHA 做 exact 比较：

```bash
python3 scripts/diagnostics/active_runtime_audit.py \
  --url http://127.0.0.1:19528 \
  --pid-file /srv/fos-coverage/candidate/.runtime-state/api.pid \
  --config /srv/fos-coverage/candidate/config/coverage_config.staging.example.json \
  --expected-revision "$(git rev-parse HEAD)" \
  --probe-database --require-live \
  > /secure/evidence/gate-f/active_runtime_audit.json
```

`configured_runtime_audit.py` 只验证配置文件，不能代替这条 live process/service、绑定配置、监听端口、release、DB identity 和 HTTP route 证据。

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
python3 scripts/diagnostics/gate_task_status.py \
  --repo-root "$PWD" \
  --matrix /secure/evidence/gate-matrix.json \
  --output /secure/evidence/gate-task-status.json
python3 scripts/diagnostics/dod_status.py \
  --repo-root "$PWD" \
  --matrix /secure/evidence/gate-matrix.json \
  --task-status /secure/evidence/gate-task-status.json \
  --output /secure/evidence/gate-dod-status.json
python3 scripts/diagnostics/release_readiness.py \
  --repo-root "$PWD" \
  --matrix /secure/evidence/gate-matrix.json \
  --task-status /secure/evidence/gate-task-status.json \
  --dod-status /secure/evidence/gate-dod-status.json \
  --risk-register /secure/evidence/gate-f/release-risk-register.json \
  --output /secure/evidence/release-readiness.json
```

只要任一外部 artifact 缺失、Gate 不匹配、commit 不匹配、runtime identity 不完整、exit code 非零或证据为 synthetic，命令就必须保持非零并输出 `INCOMPLETE`/`BLOCKED`。在真实外部证据产生前，不得将本地 Gate Matrix 的 `--allow-incomplete` 结果写成已发布或已验收。

`gate_task_status.py` 会按 Appendix B 的 80 个固定任务逐条展开结果：只有任务所属
Gate 和全部上游任务均为 `PASSED` 时，任务才会标记为 `PASSED`；缺失的真实
Candidate/MariaDB/生产证据会在每个受影响任务的 `blockers` 中保留，不能被总 Gate
状态折叠隐藏。

`dod_status.py` 会继续按第 19 节的 24 条 Definition of Done 展开结果；每条 DoD
都必须消费同一 exact-SHA task status 中列出的全部 required tasks。DoD artifact
缺失、版本不一致或任一 required task 未通过时，最终 readiness 必须保持
`NOT_READY`。

`release-risk-register.json` 必须显式绑定同一 `candidate_revision`，格式为
`{"candidate_revision":"<sha>","risks":[]}` 或列出已批准的 P2/Info 风险。
任何未关闭的 P0/P1、未批准的 P2/Info、缺失风险登记、Gate 缺证据或任务未完成都会
输出 `NOT_READY`；只有全部 Gate/任务通过时才允许输出 `READY` 或
`READY_WITH_ACCEPTED_RISK`。
