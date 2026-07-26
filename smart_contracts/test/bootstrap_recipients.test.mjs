import assert from "node:assert/strict";
import crypto from "node:crypto";
import test from "node:test";

import {
  canonicalizeRsaPublicKey,
  deriveRsaPublicKeyDer,
  dynamicWorkerInventoryFromEnvironment,
  mergeBootstrapRecipients,
  parseDynamicWorkerInventoryRecipients,
} from "../bootstrap_recipients.mjs";

const firstKeyPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const secondKeyPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const firstPublicPem = firstKeyPair.publicKey.export({ format: "pem", type: "spki" });
const secondPublicPem = secondKeyPair.publicKey.export({ format: "pem", type: "spki" });
const firstAddress = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const secondAddress = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8";

test("inventory parser selects only normalized public recipient material", () => {
  const escapedPem = firstPublicPem.replace(/\n/g, "\\n");
  const recipients = parseDynamicWorkerInventoryRecipients(JSON.stringify([{
    account_address: firstAddress,
    private_key: "must-not-be-used",
    rsa_private_key: "must-not-be-used",
    rsa_public_key: escapedPem,
  }]));

  assert.equal(recipients.length, 1);
  assert.equal(recipients[0].address, firstAddress.toLowerCase());
  assert.deepEqual(
    recipients[0].der,
    canonicalizeRsaPublicKey(firstPublicPem)
  );
  assert.deepEqual(Object.keys(recipients[0]).sort(), ["address", "der"]);
});

test("bootstrap publisher key is derived from the actual RSA signing key as DER/SPKI", () => {
  const privatePem = firstKeyPair.privateKey.export({ format: "pem", type: "pkcs8" });
  const expectedDer = firstKeyPair.publicKey.export({ format: "der", type: "spki" });

  assert.deepEqual(
    deriveRsaPublicKeyDer(privatePem, "INITIAL_GM_SIGNING_KEY"),
    expectedDer
  );
});

test("bootstrap publisher derivation rejects non-RSA and undersized signing keys", () => {
  const ecKeyPair = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const shortRsaKeyPair = crypto.generateKeyPairSync("rsa", { modulusLength: 1024 });

  assert.throws(
    () => deriveRsaPublicKeyDer(
      ecKeyPair.privateKey.export({ format: "pem", type: "pkcs8" })
    ),
    /must contain an RSA key/
  );
  assert.throws(
    () => deriveRsaPublicKeyDer(
      shortRsaKeyPair.privateKey.export({ format: "pem", type: "pkcs8" })
    ),
    /at least 2048 bits/
  );
});

test("inventory parser retains all 500 preprovisioned public recipients", () => {
  const inventory = Array.from({ length: 500 }, (_, index) => ({
    account_address: `0x${(index + 1).toString(16).padStart(40, "0")}`,
    private_key: `unused-private-key-${index}`,
    rsa_private_key: `unused-rsa-private-key-${index}`,
    rsa_public_key: firstPublicPem,
  }));
  const recipients = parseDynamicWorkerInventoryRecipients(JSON.stringify(inventory));

  assert.equal(recipients.length, 500);
  assert.equal(recipients[0].address, "0x0000000000000000000000000000000000000001");
  assert.equal(recipients[499].address, "0x00000000000000000000000000000000000001f4");
});

test("chunked inventory is reconstructed in numeric order", () => {
  const first = [{ account_address: firstAddress, rsa_public_key: firstPublicPem }];
  const second = [{ account_address: secondAddress, rsa_public_key: secondPublicPem }];
  const raw = dynamicWorkerInventoryFromEnvironment({
    DYNAMIC_WORKER_INVENTORY: "",
    DYNAMIC_WORKER_INVENTORY_001: JSON.stringify(second),
    DYNAMIC_WORKER_INVENTORY_000: JSON.stringify(first),
  });

  const recipients = parseDynamicWorkerInventoryRecipients(raw);
  assert.deepEqual(
    recipients.map((recipient) => recipient.address),
    [firstAddress.toLowerCase(), secondAddress.toLowerCase()]
  );
});

test("chunked inventory fails closed for ambiguous or incomplete input", () => {
  assert.throws(
    () => dynamicWorkerInventoryFromEnvironment({
      DYNAMIC_WORKER_INVENTORY: "[]",
      DYNAMIC_WORKER_INVENTORY_000: "[]",
    }),
    /either DYNAMIC_WORKER_INVENTORY/
  );
  assert.throws(
    () => dynamicWorkerInventoryFromEnvironment({
      DYNAMIC_WORKER_INVENTORY_001: "[]",
    }),
    /contiguous/
  );
  assert.throws(
    () => dynamicWorkerInventoryFromEnvironment({
      DYNAMIC_WORKER_INVENTORY_000: "{}",
    }),
    /JSON array/
  );
});

test("recipient merge deduplicates the same address and key across sources", () => {
  const der = canonicalizeRsaPublicKey(firstPublicPem);
  const recipients = mergeBootstrapRecipients(
    [{ address: firstAddress, der }],
    [{ address: firstAddress.toLowerCase(), der }],
    [{ address: secondAddress, der: canonicalizeRsaPublicKey(secondPublicPem) }]
  );

  assert.deepEqual(
    recipients.map((recipient) => recipient.address),
    [firstAddress.toLowerCase(), secondAddress.toLowerCase()]
  );
});

test("recipient merge rejects conflicting keys for one address", () => {
  assert.throws(
    () => mergeBootstrapRecipients(
      [{ address: firstAddress, der: canonicalizeRsaPublicKey(firstPublicPem) }],
      [{ address: firstAddress, der: canonicalizeRsaPublicKey(secondPublicPem) }]
    ),
    /Conflicting RSA public keys/
  );
});

test("inventory parser fails closed for malformed recipient data", () => {
  assert.throws(
    () => parseDynamicWorkerInventoryRecipients("{"),
    /not valid JSON/
  );
  assert.throws(
    () => parseDynamicWorkerInventoryRecipients(JSON.stringify({
      account_address: firstAddress,
      rsa_public_key: firstPublicPem,
    })),
    /must be a JSON array/
  );
  assert.throws(
    () => parseDynamicWorkerInventoryRecipients(JSON.stringify([{
      account_address: "not-an-address",
      rsa_public_key: firstPublicPem,
    }])),
    /invalid account_address/
  );
  assert.throws(
    () => parseDynamicWorkerInventoryRecipients(JSON.stringify([{
      account_address: firstAddress,
      rsa_public_key: firstKeyPair.privateKey.export({ format: "pem", type: "pkcs8" }),
    }])),
    /invalid rsa_public_key/
  );
});
