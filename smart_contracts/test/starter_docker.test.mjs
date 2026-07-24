import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";


const script = readFileSync(new URL("../starter_docker.sh", import.meta.url), "utf8");

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

test("address validation rejects a multi-line value", () => {
  const result = runFunction(
    "require_address",
    'require_address "MEDICAL_SIGNER_REGISTRY_ADDRESS" "$1"',
    [`0x${"ab".repeat(20)}\n0x${"ab".repeat(20)}`],
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stdout, /Invalid MEDICAL_SIGNER_REGISTRY_ADDRESS address/);
});
