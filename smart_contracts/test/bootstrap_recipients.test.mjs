import assert from "node:assert/strict";
import crypto from "node:crypto";
import test from "node:test";

import {
  canonicalizeRsaPublicKey,
  deriveRsaPublicKeyDer,
  normalizeRecipientAddress,
  waitForBootstrapRecipients,
} from "../bootstrap_recipients.mjs";

const firstKeyPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const secondKeyPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const firstPublicPem = firstKeyPair.publicKey.export({ format: "pem", type: "spki" });
const secondPublicPem = secondKeyPair.publicKey.export({ format: "pem", type: "spki" });
const firstAddress = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const secondAddress = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8";

test("live registry recipients normalize addresses and public keys", () => {
  assert.equal(normalizeRecipientAddress(firstAddress), firstAddress.toLowerCase());
  assert.deepEqual(
    canonicalizeRsaPublicKey(Buffer.from(firstPublicPem)),
    firstKeyPair.publicKey.export({ format: "der", type: "spki" }),
  );
  assert.deepEqual(
    canonicalizeRsaPublicKey(secondPublicPem),
    secondKeyPair.publicKey.export({ format: "der", type: "spki" }),
  );
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

test("live registry recipient normalization fails closed for malformed data", () => {
  assert.throws(
    () => normalizeRecipientAddress("not-an-address"),
    /invalid account_address/
  );
  assert.throws(
    () => canonicalizeRsaPublicKey(
      firstKeyPair.privateKey.export({ format: "pem", type: "pkcs8" }),
    ),
    /invalid rsa_public_key/
  );
});

const fakeClock = () => {
  let current = 0;
  return {
    now: () => current,
    sleep: async (milliseconds) => {
      current += milliseconds;
    },
  };
};

test("bootstrap waits until the configured number of live recipients exists", async () => {
  const clock = fakeClock();
  const observedCounts = [0, 1, 2];
  let calls = 0;

  const recipients = await waitForBootstrapRecipients(
    async () => Array.from({ length: observedCounts[calls++] }),
    {
      minimumRecipients: 2,
      pollIntervalMs: 10,
      timeoutMs: 100,
      now: clock.now,
      sleep: clock.sleep,
    },
  );

  assert.equal(recipients.length, 2);
  assert.equal(calls, 3);
  assert.equal(clock.now(), 20);
});

test("bootstrap waits for the exact recipient set declared by the Control API", async () => {
  const clock = fakeClock();
  const first = {
    address: firstAddress.toLowerCase(),
    der: firstKeyPair.publicKey.export({ format: "der", type: "spki" }),
  };
  const second = {
    address: secondAddress.toLowerCase(),
    der: secondKeyPair.publicKey.export({ format: "der", type: "spki" }),
  };
  const observations = [[first], [first], [first, second]];
  let calls = 0;

  const recipients = await waitForBootstrapRecipients(
    async () => observations[calls++],
    {
      requiredRecipientAddresses: [secondAddress, firstAddress],
      pollIntervalMs: 10,
      timeoutMs: 100,
      now: clock.now,
      sleep: clock.sleep,
    },
  );

  assert.deepEqual(
    recipients.map((recipient) => recipient.address),
    [secondAddress.toLowerCase(), firstAddress.toLowerCase()],
  );
  assert.equal(calls, 3);
});

test("declared bootstrap recipient sets reject duplicates", async () => {
  await assert.rejects(
    waitForBootstrapRecipients(async () => [], {
      requiredRecipientAddresses: [firstAddress, firstAddress.toLowerCase()],
    }),
    /must not contain duplicates/,
  );
});

test("bootstrap requires the recipient threshold throughout the settle interval", async () => {
  const clock = fakeClock();
  const observedCounts = [2, 1, 2, 3, 3, 3];
  let calls = 0;

  const recipients = await waitForBootstrapRecipients(
    async () => Array.from({ length: observedCounts[calls++] }),
    {
      minimumRecipients: 2,
      registrationSettleMs: 20,
      pollIntervalMs: 10,
      timeoutMs: 100,
      now: clock.now,
      sleep: clock.sleep,
    },
  );

  assert.equal(recipients.length, 3);
  assert.equal(calls, 6);
  assert.equal(clock.now(), 50);
});

test("bootstrap restarts its quiet period when a recipient key changes", async () => {
  const clock = fakeClock();
  const first = [{ address: firstAddress, der: Buffer.from("first") }];
  const changed = [{ address: firstAddress, der: Buffer.from("changed") }];
  const observations = [first, changed, changed, changed];
  let calls = 0;

  const recipients = await waitForBootstrapRecipients(
    async () => observations[calls++],
    {
      minimumRecipients: 1,
      registrationSettleMs: 20,
      pollIntervalMs: 10,
      timeoutMs: 100,
      now: clock.now,
      sleep: clock.sleep,
    },
  );

  assert.deepEqual(recipients, changed);
  assert.equal(calls, 4);
  assert.equal(clock.now(), 30);
});

test("bootstrap recipient wait fails closed on timeout", async () => {
  const clock = fakeClock();

  await assert.rejects(
    waitForBootstrapRecipients(
      async () => [firstAddress],
      {
        minimumRecipients: 2,
        pollIntervalMs: 10,
        timeoutMs: 25,
        now: clock.now,
        sleep: clock.sleep,
      },
    ),
    /Timed out waiting for 2 live DeviceRegistry recipient.*last observed 1/,
  );
  assert.equal(clock.now(), 25);
});

test("bootstrap recipient wait validates its timing and loader inputs", async () => {
  await assert.rejects(
    waitForBootstrapRecipients(async () => [], { minimumRecipients: 0 }),
    /minimumRecipients/,
  );
  await assert.rejects(
    waitForBootstrapRecipients(async () => [], {
      registrationSettleMs: 11,
      timeoutMs: 10,
    }),
    /must not exceed/,
  );
  await assert.rejects(
    waitForBootstrapRecipients(async () => null),
    /must return an array/,
  );
});
