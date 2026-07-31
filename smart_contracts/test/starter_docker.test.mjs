import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";


const script = readFileSync(new URL("../starter_docker.sh", import.meta.url), "utf8");
const encryptedBootstrapScript = readFileSync(
  new URL("../bootstrap_encrypted_gm.mjs", import.meta.url),
  "utf8",
);
const phalaMain = readFileSync(new URL("../../phala/main.tf", import.meta.url), "utf8");
const contractsComposeTemplate = readFileSync(
  new URL("../../phala/dstack-compose.contracts.phala.tftpl", import.meta.url),
  "utf8",
);
const localCompose = readFileSync(new URL("../../compose.yml", import.meta.url), "utf8");
const staticWorkerTemplate = readFileSync(
  new URL("../../phala/dstack-compose.worker.phala.tftpl", import.meta.url),
  "utf8",
);
const dynamicWorkerTemplate = readFileSync(
  new URL("../../phala/dynamic-workers/worker-compose.tftpl", import.meta.url),
  "utf8",
);

function shellFunction(name) {
  const start = script.indexOf(`${name}() {`);
  assert.notEqual(start, -1, `${name} must exist in starter_docker.sh`);
  const end = script.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `${name} must have a closing brace`);
  return script.slice(start, end + 3);
}

function runFunction(functionName, invocation, args = []) {
  return spawnSync(
    "bash",
    ["-c", `${shellFunction(functionName)}\n${invocation}`, "--", ...args],
    { encoding: "utf8" },
  );
}

test("deployment address selection ignores calls to the deployed contract", () => {
  const directory = mkdtempSync(join(tmpdir(), "starter-address-"));
  const fixture = join(directory, "run-latest.json");
  writeFileSync(
    fixture,
    JSON.stringify({
      transactions: [
        {
          transactionType: "CREATE",
          contractName: "MedicalSignerRegistry",
          contractAddress: "0x" + "ab".repeat(20),
        },
        {
          transactionType: "CALL",
          contractName: "MedicalSignerRegistry",
          contractAddress: "0x" + "ab".repeat(20),
        },
      ],
    }),
  );

  const result = runFunction(
    "deployed_contract_address",
    'deployed_contract_address "MedicalSignerRegistry" "$1"',
    [fixture],
  );

  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "0x" + "ab".repeat(20));
});

test("deployment address selection fails closed for duplicate deployments", () => {
  const directory = mkdtempSync(join(tmpdir(), "starter-address-"));
  const fixture = join(directory, "run-latest.json");
  writeFileSync(
    fixture,
    JSON.stringify({
      transactions: [
        {
          transactionType: "CREATE",
          contractName: "DeviceRegistry",
          contractAddress: "0x" + "11".repeat(20),
        },
        {
          transactionType: "CREATE2",
          contractName: "DeviceRegistry",
          contractAddress: "0x" + "22".repeat(20),
        },
      ],
    }),
  );

  const result = runFunction(
    "deployed_contract_address",
    'deployed_contract_address "DeviceRegistry" "$1"',
    [fixture],
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /expected exactly one deployment/);
});

test("contract receipt accounting keeps receipt fees and Mainnet scenarios separate", () => {
  const directory = mkdtempSync(join(tmpdir(), "starter-cost-"));
  const fixture = join(directory, "run-latest.json");
  const costCsv = join(directory, "transaction-costs.csv");
  writeFileSync(
    fixture,
    JSON.stringify({
      receipts: [
        {
          gasUsed: "21000",
          effectiveGasPrice: "1234567890",
          transactionHash: `0x${"12".repeat(32)}`,
          blockNumber: "1",
          from: `0x${"34".repeat(20)}`,
          to: `0x${"56".repeat(20)}`,
          contractAddress: null,
        },
      ],
    }),
  );

  const result = runFunction(
    "log_broadcast_gas_cost",
    [
      "ETH_EUR_PRICE=3000.25",
      "ETH_USD_PRICE=3500.5",
      "EXCHANGE_RATE_SOURCE=exchange-rate-fixture",
      "EXCHANGE_RATE_TIMESTAMP_UTC=2026-06-29T00:00:00Z",
      "REFERENCE_MAINNET_GAS_PRICE_GWEI=0.9291",
      "REFERENCE_GAS_PRICE_SOURCE=gas-price-fixture",
      "REFERENCE_GAS_PRICE_TIMESTAMP_UTC=2026-06-29T00:00:00Z",
      'TRANSACTION_COST_CSV="$2"',
      'log_broadcast_gas_cost "test" "$1"',
    ].join("\n"),
    [fixture, costCsv],
  );

  assert.equal(result.status, 0, result.stderr);
  const event = JSON.parse(result.stdout.trim());
  assert.equal(event.gasUsed, 21000);
  assert.equal(event.costWei, "25925925690000");
  assert.equal(event.costGwei, "25925.92569");
  assert.equal(event.costEth, "0.00002592592569");
  assert.equal(event.costEur, "0.0777842605514225");
  assert.equal(event.costUsd, "0.090753704981845");
  assert.equal(event.feeBasis, "receipt.effectiveGasPrice");
  assert.equal(event.valuationKind, "receipt_fee_fiat_estimate");
  assert.equal(event.mainnetEstimateWei, "19511100000000");
  assert.equal(event.mainnetEstimateEur, "0.058538177775");
  assert.equal(event.mainnetEstimateKind, "counterfactual_mainnet_equivalent");
});

test("address validation rejects a multi-line value", () => {
  const result = runFunction(
    "require_address",
    'require_address "MEDICAL_SIGNER_REGISTRY_ADDRESS" "$1"',
    [`0x${"ab".repeat(20)}\n0x${"ab".repeat(20)}`],
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stdout, /Invalid MEDICAL_SIGNER_REGISTRY_ADDRESS address/);
});

test("runtime publishes admission metadata before bootstrap and readiness afterward", () => {
  const imagePolicy = script.lastIndexOf("setExpectedWorkerImageDigest(bytes32)");
  const rolePolicies = script.lastIndexOf("\nprovision_worker_policy_hashes\n");
  const contracts = script.lastIndexOf("\npublish_runtime_contract_manifest\n");
  const admission = script.lastIndexOf("\npublish_runtime_admission_ready_marker\n");
  const declaration = script.lastIndexOf("\nwait_for_bootstrap_recipient_declaration\n");
  const bootstrap = script.lastIndexOf("\nprepare_encrypted_initial_gm\n");
  const ready = script.lastIndexOf("\npublish_runtime_ready_marker\n");

  assert.notEqual(imagePolicy, -1);
  assert.notEqual(rolePolicies, -1);
  assert.notEqual(contracts, -1);
  assert.notEqual(admission, -1);
  assert.notEqual(declaration, -1);
  assert.notEqual(bootstrap, -1);
  assert.notEqual(ready, -1);
  assert.ok(imagePolicy < contracts, "worker image policy must precede admission");
  assert.ok(imagePolicy < rolePolicies, "image policy must precede role-policy provisioning");
  assert.ok(rolePolicies < contracts, "role-policy provisioning must precede admission");
  assert.ok(contracts < admission, "contract manifest must precede admission marker");
  assert.ok(admission < declaration, "admission marker must precede recipient declaration");
  assert.ok(declaration < bootstrap, "recipient declaration must precede encrypted bootstrap");
  assert.ok(bootstrap < ready, "ready marker must follow encrypted bootstrap");
});

test("worker policies are pre-rendered from the dynamic worker template", () => {
  assert.match(
    phalaMain,
    /dynamic-workers\/worker-compose\.tftpl[\s\S]*inference_enabled = false/,
  );
  assert.match(
    phalaMain,
    /dynamic-workers\/worker-compose\.tftpl[\s\S]*inference_enabled = true/,
  );
  assert.match(phalaMain, /TRAINING_WORKER_POLICY_APP_COMPOSE_B64\s*=\s*base64encode/);
  assert.match(phalaMain, /INFERENCE_WORKER_POLICY_APP_COMPOSE_B64\s*=\s*base64encode/);
  assert.match(
    contractsComposeTemplate,
    /TRAINING_WORKER_POLICY_APP_COMPOSE_B64: "\$\$\{TRAINING_WORKER_POLICY_APP_COMPOSE_B64\}"/,
  );
  assert.match(
    contractsComposeTemplate,
    /INFERENCE_WORKER_POLICY_APP_COMPOSE_B64: "\$\$\{INFERENCE_WORKER_POLICY_APP_COMPOSE_B64\}"/,
  );
});

test("static and dynamic worker templates cannot drift outside variable telemetry", () => {
  const normalize = (template) => template
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("#") && line.trim() !== "")
    .map((line) => line.startsWith("      DFL_TELEMETRY_URL:")
      ? "      DFL_TELEMETRY_URL: <variable>"
      : line)
    .join("\n");
  assert.equal(normalize(staticWorkerTemplate), normalize(dynamicWorkerTemplate));
});

test("worker policy derivation rejects missing pre-provisioned input", () => {
  const result = runFunction(
    "derive_worker_policy_hash",
    'derive_worker_policy_hash "training-only" ""',
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /refusing trust-on-first-use/);
});

test("local mock derives deterministic policy references without weakening Phala", () => {
  const digest = "78".repeat(32);
  const result = runFunction(
    "configure_local_mock_worker_policy_references",
    [
      "LOCAL_TDX_MOCK=1",
      `EXPECTED_WORKER_IMAGE_DIGEST=${digest}`,
      "configure_local_mock_worker_policy_references",
      "printf '%s\\n%s\\n' \"$TRAINING_WORKER_POLICY_APP_COMPOSE_B64\" \"$INFERENCE_WORKER_POLICY_APP_COMPOSE_B64\"",
    ].join("\n"),
  );
  assert.equal(result.status, 0, result.stderr);
  const [training, inference] = result.stdout.trim().split("\n");
  assert.ok(training);
  assert.ok(inference);
  assert.notEqual(training, inference);
  const decodedTraining = JSON.parse(Buffer.from(training, "base64").toString("utf8"));
  assert.match(
    decodedTraining.docker_compose_file,
    new RegExp(`local/dfl-worker@sha256:${digest}`),
  );
  assert.doesNotMatch(decodedTraining.docker_compose_file, /ports:/);
  assert.match(
    JSON.parse(Buffer.from(inference, "base64").toString("utf8")).docker_compose_file,
    /ports:\n\s+- "8080:8080"/,
  );
  assert.match(localCompose, /TRAINING_WORKER_POLICY_APP_COMPOSE_B64:/);
  assert.match(localCompose, /INFERENCE_WORKER_POLICY_APP_COMPOSE_B64:/);
});

test("stale bootstrap markers are all cleared before deployment", () => {
  const result = runFunction(
    "clear_runtime_bootstrap_markers",
    [
      "IPFS_PROVIDER=kubo",
      "KUBO_API_URL=http://kubo:5001",
      "curl() { printf '%s\\n' \"$*\" >&2; }",
      "clear_runtime_bootstrap_markers",
    ].join("\n"),
  );

  assert.equal(result.status, 0, result.stderr);
  assert.ok(result.stderr.includes("arg=/runtime/contracts.json"));
  assert.ok(result.stderr.includes("arg=/runtime/admission-ready.json"));
  assert.ok(result.stderr.includes("arg=/runtime/bootstrap-recipients.json"));
  assert.ok(result.stderr.includes("arg=/runtime/ready.json"));
});

test("encrypted bootstrap uses only live DeviceRegistry recipients", () => {
  assert.doesNotMatch(encryptedBootstrapScript, /DYNAMIC_WORKER_INVENTORY/);
  assert.doesNotMatch(encryptedBootstrapScript, /--bootstrap-address/);
  assert.doesNotMatch(encryptedBootstrapScript, /--bootstrap-public-key/);
  assert.doesNotMatch(script, /INITIAL_BOOTSTRAP_RECIPIENT/);
  assert.match(encryptedBootstrapScript, /waitForBootstrapRecipients/);
  assert.match(encryptedBootstrapScript, /loadRecipientsFromRegistry/);
  assert.match(encryptedBootstrapScript, /--required-recipients-file/);
});

test("bootstrap wait settings are forwarded with safe defaults", () => {
  const bootstrapFunction = shellFunction("prepare_encrypted_initial_gm");

  assert.match(bootstrapFunction, /BOOTSTRAP_MIN_RECIPIENTS:-1/);
  assert.match(bootstrapFunction, /BOOTSTRAP_REGISTRATION_SETTLE_SECONDS:-0/);
  assert.match(bootstrapFunction, /BOOTSTRAP_REGISTRATION_TIMEOUT_SECONDS:-900/);
  assert.match(bootstrapFunction, /BOOTSTRAP_REGISTRATION_POLL_SECONDS:-2/);
  assert.match(bootstrapFunction, /--minimum-recipients/);
  assert.match(bootstrapFunction, /--registration-settle-seconds/);
});
