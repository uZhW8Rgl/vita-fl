import crypto from "node:crypto";

const ADDRESS_PATTERN = /^0x[0-9a-fA-F]{40}$/;
const PUBLIC_KEY_PEM_PATTERN = /-----BEGIN (?:RSA )?PUBLIC KEY-----/;
const PRIVATE_KEY_PEM_PATTERN = /-----BEGIN (?:ENCRYPTED |RSA )?PRIVATE KEY-----/;

export const normalizeRecipientAddress = (value, context = "recipient") => {
  if (typeof value !== "string") {
    throw new Error(`${context} account_address must be a string.`);
  }

  const address = value.trim();
  if (!ADDRESS_PATTERN.test(address)) {
    throw new Error(`${context} has an invalid account_address.`);
  }
  return address.toLowerCase();
};

export const canonicalizeRsaPublicKey = (value, context = "recipient") => {
  let publicKey;

  try {
    if (typeof value === "string") {
      const pem = value
        .trim()
        .replace(/\\r\\n/g, "\n")
        .replace(/\\n/g, "\n")
        .replace(/\r\n/g, "\n");
      if (!pem || PRIVATE_KEY_PEM_PATTERN.test(pem) || !PUBLIC_KEY_PEM_PATTERN.test(pem)) {
        throw new Error("expected an RSA public-key PEM");
      }
      publicKey = crypto.createPublicKey(pem);
    } else if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
      const keyBytes = Buffer.from(value);
      const text = keyBytes.toString("utf8");
      if (text.includes("-----BEGIN")) {
        if (PRIVATE_KEY_PEM_PATTERN.test(text) || !PUBLIC_KEY_PEM_PATTERN.test(text)) {
          throw new Error("expected an RSA public-key PEM");
        }
        publicKey = crypto.createPublicKey(text.replace(/\r\n/g, "\n"));
      } else {
        publicKey = crypto.createPublicKey({
          key: keyBytes,
          format: "der",
          type: "spki",
        });
      }
    } else {
      throw new Error("public key must be a PEM string or DER byte sequence");
    }
  } catch (error) {
    throw new Error(`${context} has an invalid rsa_public_key: ${error.message}`);
  }

  if (publicKey.asymmetricKeyType !== "rsa") {
    throw new Error(`${context} rsa_public_key must contain an RSA key.`);
  }
  const modulusLength = publicKey.asymmetricKeyDetails?.modulusLength;
  if (!Number.isInteger(modulusLength) || modulusLength < 2048) {
    throw new Error(`${context} rsa_public_key must use an RSA modulus of at least 2048 bits.`);
  }

  return publicKey.export({ format: "der", type: "spki" });
};

export const deriveRsaPublicKeyDer = (value, context = "signing key") => {
  let privateKey;

  try {
    privateKey = crypto.createPrivateKey(value);
  } catch (error) {
    throw new Error(`${context} is not a valid private key: ${error.message}`);
  }

  if (privateKey.asymmetricKeyType !== "rsa") {
    throw new Error(`${context} must contain an RSA key.`);
  }
  const modulusLength = privateKey.asymmetricKeyDetails?.modulusLength;
  if (!Number.isInteger(modulusLength) || modulusLength < 2048) {
    throw new Error(`${context} must use an RSA modulus of at least 2048 bits.`);
  }

  return crypto.createPublicKey(privateKey).export({
    format: "der",
    type: "spki",
  });
};

const assertInteger = (name, value, minimum) => {
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer greater than or equal to ${minimum}.`);
  }
};

const normalizeRequiredRecipientAddresses = (values) => {
  if (values === undefined || values === null) {
    return null;
  }
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error("requiredRecipientAddresses must be a non-empty array.");
  }

  const normalized = values.map((value, index) => (
    normalizeRecipientAddress(value, `required recipient ${index}`)
  ));
  if (new Set(normalized).size !== normalized.length) {
    throw new Error("requiredRecipientAddresses must not contain duplicates.");
  }
  return normalized;
};

const defaultSleep = (milliseconds) => new Promise((resolve) => {
  setTimeout(resolve, milliseconds);
});

const recipientSetFingerprint = (recipients) => JSON.stringify(
  recipients.map((recipient) => {
    if (!recipient || typeof recipient !== "object") {
      return recipient ?? null;
    }
    const address = typeof recipient.address === "string"
      ? recipient.address.toLowerCase()
      : "";
    const key = recipient.der instanceof Uint8Array
      ? Buffer.from(recipient.der).toString("hex")
      : "";
    return [address, key];
  }).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)))
);

export const waitForBootstrapRecipients = async (
  loadRecipients,
  {
    minimumRecipients = 1,
    registrationSettleMs = 0,
    timeoutMs = 15 * 60 * 1000,
    pollIntervalMs = 2 * 1000,
    requiredRecipientAddresses = null,
    sleep = defaultSleep,
    now = Date.now,
    onProgress = () => {},
  } = {},
) => {
  if (typeof loadRecipients !== "function") {
    throw new Error("loadRecipients must be a function.");
  }
  if (typeof sleep !== "function" || typeof now !== "function" || typeof onProgress !== "function") {
    throw new Error("sleep, now and onProgress must be functions.");
  }
  assertInteger("minimumRecipients", minimumRecipients, 1);
  assertInteger("registrationSettleMs", registrationSettleMs, 0);
  assertInteger("timeoutMs", timeoutMs, 1);
  assertInteger("pollIntervalMs", pollIntervalMs, 1);
  if (registrationSettleMs > timeoutMs) {
    throw new Error("registrationSettleMs must not exceed timeoutMs.");
  }
  const requiredAddresses = normalizeRequiredRecipientAddresses(
    requiredRecipientAddresses
  );
  const requiredAddressSet = requiredAddresses === null
    ? null
    : new Set(requiredAddresses);

  const startedAt = now();
  let thresholdReachedAt = null;
  let settledRecipientSet = null;

  while (true) {
    const recipients = await loadRecipients();
    if (!Array.isArray(recipients)) {
      throw new Error("DeviceRegistry recipient loader must return an array.");
    }

    const observedAt = now();
    const recipientsByAddress = new Map();
    for (const recipient of recipients) {
      if (!recipient || typeof recipient !== "object") {
        continue;
      }
      const normalizedAddress = normalizeRecipientAddress(
        recipient.address,
        "DeviceRegistry recipient"
      );
      if (recipientsByAddress.has(normalizedAddress)) {
        throw new Error(
          `DeviceRegistry recipient loader returned duplicate address ${normalizedAddress}.`
        );
      }
      recipientsByAddress.set(normalizedAddress, recipient);
    }

    if (requiredAddressSet !== null) {
      const missing = requiredAddresses.filter(
        (address) => !recipientsByAddress.has(address)
      );
      if (missing.length === 0) {
        return requiredAddresses.map((address) => recipientsByAddress.get(address));
      }
      onProgress({
        recipientCount: requiredAddresses.length - missing.length,
        minimumRecipients: requiredAddresses.length,
        phase: "waiting-required",
        missingAddresses: missing,
        remainingMs: null,
      });
    } else if (recipients.length >= minimumRecipients) {
      const fingerprint = recipientSetFingerprint(recipients);
      if (thresholdReachedAt === null || fingerprint !== settledRecipientSet) {
        thresholdReachedAt = observedAt;
        settledRecipientSet = fingerprint;
      }
      const settledForMs = observedAt - thresholdReachedAt;
      if (settledForMs >= registrationSettleMs) {
        return recipients;
      }
      onProgress({
        recipientCount: recipients.length,
        minimumRecipients,
        phase: "settling",
        remainingMs: registrationSettleMs - settledForMs,
      });
    } else {
      thresholdReachedAt = null;
      settledRecipientSet = null;
      onProgress({
        recipientCount: recipients.length,
        minimumRecipients,
        phase: "waiting",
        remainingMs: null,
      });
    }

    const elapsedMs = observedAt - startedAt;
    if (elapsedMs >= timeoutMs) {
      throw new Error(
        requiredAddressSet === null
          ? `Timed out waiting for ${minimumRecipients} live DeviceRegistry recipient(s); `
            + `last observed ${recipients.length}.`
          : `Timed out waiting for the declared DeviceRegistry recipient set; `
            + `last observed ${recipientsByAddress.size} of ${requiredAddresses.length}.`
      );
    }

    const timeoutRemainingMs = timeoutMs - elapsedMs;
    const settleRemainingMs = thresholdReachedAt === null
      ? pollIntervalMs
      : Math.max(1, registrationSettleMs - (observedAt - thresholdReachedAt));
    await sleep(Math.min(pollIntervalMs, settleRemainingMs, timeoutRemainingMs));
  }
};
