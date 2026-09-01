#!/usr/bin/env node
/*
 * Combine two independently produced, exact-revision browser workloads into
 * release performance evidence.
 *
 * This command deliberately does not run a benchmark.  The baseline and the
 * candidate must already have been measured from isolated checkouts.  Keeping
 * the combine step separate prevents a same-process DOM comparison from being
 * accidentally promoted to release evidence.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const REQUIRED_TIERS = ['Tier_A_1k', 'Tier_B_10k', 'Tier_C_50k', 'Tier_D_100k'];
const EPHEMERAL_ENV_KEYS = new Set([
  'ci_run', 'run_id', 'workflow_run_id', 'started_at', 'finished_at',
  'timestamp'
]);

function usage() {
  return [
    'Usage:',
    '  node scripts/diagnostics/release_performance_ab.js',
    '    --baseline-artifact <json>',
    '    --candidate-artifact <json>',
    '    --baseline-commit <sha>',
    '    --candidate-commit <sha>',
    '    --workload-hash <hash>',
    '    --output <json>',
    '    [--release-validation-session-id <attempt-id>]',
    '    [--candidate-artifact-sha256 <sha256>]',
    '    [--served-root-sha256 <sha256>]',
    '    [--max-regression-percent <number>]',
    '',
    'Each input must be an independently measured release_performance_revision',
    'artifact. synthetic_dom_microbenchmark output is intentionally rejected.'
  ].join('\n');
}

function parseArgs(argv) {
  const args = {};
  const valueArgs = new Set([
    '--baseline-artifact', '--candidate-artifact', '--baseline-commit',
    '--candidate-commit', '--workload-hash', '--output',
    '--max-regression-percent', '--release-validation-session-id',
    '--candidate-artifact-sha256', '--served-root-sha256'
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!valueArgs.has(arg)) {
      throw new Error(`unknown argument: ${arg}`);
    }
    if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw new Error(`missing value for ${arg}`);
    }
    args[arg.slice(2).replace(/-/g, '_')] = argv[index + 1];
    index += 1;
  }
  return args;
}

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (!value || typeof value !== 'object') return JSON.stringify(value);
  return `{${Object.keys(value).sort().map(key =>
    `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
}

function comparableEnvironment(environment) {
  if (!environment || typeof environment !== 'object' || Array.isArray(environment)) {
    return null;
  }
  const clone = {};
  Object.keys(environment).forEach(key => {
    if (!EPHEMERAL_ENV_KEYS.has(key)) clone[key] = environment[key];
  });
  return stableJson(clone);
}

function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
}

function readRevisionArtifact(filePath, expectedRevision, expectedWorkloadHash, role) {
  if (!filePath) throw new Error(`${role} artifact path is required`);
  const absolutePath = path.resolve(filePath);
  if (!fs.existsSync(absolutePath) || !fs.statSync(absolutePath).isFile()) {
    throw new Error(`${role} artifact does not exist: ${absolutePath}`);
  }
  let artifact;
  try {
    artifact = JSON.parse(fs.readFileSync(absolutePath, 'utf8'));
  } catch (error) {
    throw new Error(`${role} artifact is not valid JSON: ${error.message}`);
  }
  if (!artifact || typeof artifact !== 'object' || Array.isArray(artifact)) {
    throw new Error(`${role} artifact must be a JSON object`);
  }
  if (artifact.status !== 'PASSED') {
    throw new Error(`${role} artifact status is not PASSED`);
  }
  if (artifact.evidence_class !== 'release_performance_revision' ||
      artifact.comparison_type !== 'single_revision') {
    throw new Error(
      `${role} artifact is not an independent release_performance_revision`
    );
  }
  if (!expectedRevision || artifact.revision !== expectedRevision) {
    throw new Error(`${role} artifact revision does not match --${role}-commit`);
  }
  if (!artifact.workload_id || artifact.workload_hash !== expectedWorkloadHash) {
    throw new Error(`${role} artifact workload identity does not match the requested hash`);
  }
  if (artifact.exit_code !== undefined && artifact.exit_code !== 0) {
    throw new Error(`${role} artifact has a non-zero exit_code`);
  }
  if (!comparableEnvironment(artifact.environment_identity)) {
    throw new Error(`${role} artifact has no environment_identity`);
  }
  if (artifact.baseline_commit || artifact.candidate_commit ||
      artifact.comparison_type === 'synthetic_same_run' ||
      artifact.evidence_class === 'synthetic_dom_microbenchmark') {
    throw new Error(`${role} artifact contains same-run/synthetic A/B identity`);
  }

  const tiers = artifact.tiers || artifact;
  REQUIRED_TIERS.forEach(tierName => {
    const tier = tiers[tierName];
    if (!tier || tier.status !== 'PASSED' || !isFiniteNumber(tier.measured_ms)) {
      throw new Error(`${role} artifact tier ${tierName} lacks measured_ms/PASSED`);
    }
  });
  const virtual = artifact.coverage_virtual_scroll_100k;
  if (!virtual || virtual.status !== 'PASSED' || !isFiniteNumber(virtual.elapsed_ms)) {
    throw new Error(`${role} artifact lacks the 100k virtual-scroll workload`);
  }
  return {
    artifact,
    path: absolutePath,
    sha256: sha256File(absolutePath),
    tiers,
    environmentKey: comparableEnvironment(artifact.environment_identity)
  };
}

function regressionPercent(baseline, candidate) {
  if (baseline === 0) return candidate === 0 ? 0 : Infinity;
  return ((candidate - baseline) / baseline) * 100;
}

function rounded(value) {
  return Number.isFinite(value) ? Number(value.toFixed(3)) : null;
}

function writeOutput(outputPath, result) {
  if (!outputPath) return;
  const absolutePath = path.resolve(outputPath);
  fs.mkdirSync(path.dirname(absolutePath), { recursive: true });
  fs.writeFileSync(absolutePath, JSON.stringify(result, null, 2));
}

function failure(message, outputPath, extra) {
  const result = Object.assign({
    status: 'FAILED',
    evidence_class: 'release_performance_ab',
    comparison_type: 'release_revision_ab',
    violations: [message],
    exit_code: 1
  }, extra || {});
  writeOutput(outputPath, result);
  process.stderr.write(`${message}\n`);
  return result;
}

function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    failure(`${error.message}\n${usage()}`);
    process.exitCode = 2;
    return;
  }
  const outputPath = args.output || '';
  const required = [
    ['baseline_artifact', '--baseline-artifact'],
    ['candidate_artifact', '--candidate-artifact'],
    ['baseline_commit', '--baseline-commit'],
    ['candidate_commit', '--candidate-commit'],
    ['workload_hash', '--workload-hash'],
    ['output', '--output']
  ];
  const missing = required.filter(item => !args[item[0]]).map(item => item[1]);
  if (missing.length) {
    failure(`missing required arguments: ${missing.join(', ')}\n${usage()}`, outputPath);
    process.exitCode = 2;
    return;
  }
  if (args.baseline_commit === args.candidate_commit) {
    failure('baseline and candidate commits must be different', outputPath);
    process.exitCode = 2;
    return;
  }
  const budget = args.max_regression_percent === undefined
    ? 20
    : Number(args.max_regression_percent);
  if (!Number.isFinite(budget) || budget < 0) {
    failure('--max-regression-percent must be a finite non-negative number', outputPath);
    process.exitCode = 2;
    return;
  }
  const bindingFields = [
    'release_validation_session_id', 'candidate_artifact_sha256', 'served_root_sha256'
  ];
  const bindingCount = bindingFields.filter(name => Boolean(args[name])).length;
  if (bindingCount !== 0 && bindingCount !== bindingFields.length) {
    failure('attempt publication binding requires all three identity fields', outputPath);
    process.exitCode = 2;
    return;
  }

  let baseline;
  let candidate;
  try {
    baseline = readRevisionArtifact(
      args.baseline_artifact, args.baseline_commit, args.workload_hash, 'baseline'
    );
    candidate = readRevisionArtifact(
      args.candidate_artifact, args.candidate_commit, args.workload_hash, 'candidate'
    );
    if (baseline.path === candidate.path) {
      throw new Error('baseline and candidate artifacts must be separate files');
    }
    if (baseline.environmentKey !== candidate.environmentKey) {
      throw new Error('baseline and candidate environment_identity do not match');
    }
    if (baseline.artifact.workload_id !== candidate.artifact.workload_id) {
      throw new Error('baseline and candidate workload_id do not match');
    }
  } catch (error) {
    failure(error.message, outputPath, {
      baseline_commit: args.baseline_commit,
      candidate_commit: args.candidate_commit,
      workload_hash: args.workload_hash
    });
    process.exitCode = 1;
    return;
  }

  const violations = [];
  const tiers = {};
  REQUIRED_TIERS.forEach(tierName => {
    const baselineMs = baseline.tiers[tierName].measured_ms;
    const candidateMs = candidate.tiers[tierName].measured_ms;
    const regression = regressionPercent(baselineMs, candidateMs);
    const withinBudget = Number.isFinite(regression) && regression <= budget;
    if (!withinBudget) violations.push(`${tierName} exceeded the ${budget}% regression budget`);
    tiers[tierName] = {
      status: withinBudget ? 'PASSED' : 'FAILED',
      baseline_ms: rounded(baselineMs),
      candidate_ms: rounded(candidateMs),
      regression_percent: rounded(regression),
      regression_budget_percent: budget,
      workload_id: baseline.artifact.workload_id,
      baseline_revision: args.baseline_commit,
      candidate_revision: args.candidate_commit
    };
  });

  const baselineVirtual = baseline.artifact.coverage_virtual_scroll_100k;
  const candidateVirtual = candidate.artifact.coverage_virtual_scroll_100k;
  const virtualRegression = regressionPercent(
    baselineVirtual.elapsed_ms, candidateVirtual.elapsed_ms
  );
  if (!Number.isFinite(virtualRegression) || virtualRegression > budget) {
    violations.push(`coverage_virtual_scroll_100k exceeded the ${budget}% regression budget`);
  }
  const virtual = Object.assign({}, candidateVirtual, {
    status: violations.some(item => item.indexOf('coverage_virtual_scroll_100k') >= 0)
      ? 'FAILED' : 'PASSED',
    baseline_elapsed_ms: rounded(baselineVirtual.elapsed_ms),
    candidate_elapsed_ms: rounded(candidateVirtual.elapsed_ms),
    regression_percent: rounded(virtualRegression),
    regression_budget_percent: budget,
    baseline_revision: args.baseline_commit,
    candidate_revision: args.candidate_commit
  });

  const result = {
    status: violations.length ? 'FAILED' : 'PASSED',
    evidence_class: 'release_performance_ab',
    comparison_type: 'release_revision_ab',
    workload_id: baseline.artifact.workload_id,
    workload_hash: args.workload_hash,
    baseline_commit: args.baseline_commit,
    candidate_commit: args.candidate_commit,
    release_validation_session_id: args.release_validation_session_id || '',
    candidate_artifact_sha256: args.candidate_artifact_sha256 || '',
    served_root_sha256: args.served_root_sha256 || '',
    environment_identity: baseline.artifact.environment_identity,
    baseline_artifact: { path: baseline.path, sha256: baseline.sha256 },
    candidate_artifact: { path: candidate.path, sha256: candidate.sha256 },
    source_inputs_sha256: [baseline.sha256, candidate.sha256],
    regression_budget_percent: budget,
    baseline_ms: tiers.Tier_B_10k.baseline_ms,
    candidate_ms: tiers.Tier_B_10k.candidate_ms,
    regression_percent: tiers.Tier_B_10k.regression_percent,
    tiers,
    Tier_A_1k: tiers.Tier_A_1k,
    Tier_B_10k: tiers.Tier_B_10k,
    Tier_C_50k: tiers.Tier_C_50k,
    Tier_D_100k: tiers.Tier_D_100k,
    coverage_virtual_scroll_100k: virtual,
    source_artifacts: {
      baseline: { path: baseline.path, sha256: baseline.sha256, revision: args.baseline_commit },
      candidate: { path: candidate.path, sha256: candidate.sha256, revision: args.candidate_commit }
    },
    command: `node scripts/diagnostics/release_performance_ab.js ${process.argv.slice(2).join(' ')}`,
    exit_code: violations.length ? 1 : 0,
    violations
  };
  writeOutput(outputPath, result);
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (violations.length) process.exitCode = 1;
}

main();
