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

export const parseDynamicWorkerInventoryRecipients = (rawInventory) => {
  if (rawInventory === undefined || rawInventory === null || String(rawInventory).trim() === "") {
    return [];
  }

  let inventory;
  try {
    inventory = JSON.parse(String(rawInventory));
  } catch (error) {
    throw new Error(`DYNAMIC_WORKER_INVENTORY is not valid JSON: ${error.message}`);
  }
  if (!Array.isArray(inventory)) {
    throw new Error("DYNAMIC_WORKER_INVENTORY must be a JSON array.");
  }

  return inventory.map((entry, index) => {
    const context = `DYNAMIC_WORKER_INVENTORY[${index}]`;
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      throw new Error(`${context} must be an object.`);
    }
    if (!Object.hasOwn(entry, "account_address") || !Object.hasOwn(entry, "rsa_public_key")) {
      throw new Error(`${context} must define account_address and rsa_public_key.`);
    }

    return {
      address: normalizeRecipientAddress(entry.account_address, context),
      der: canonicalizeRsaPublicKey(entry.rsa_public_key, context),
    };
  });
};

export const mergeBootstrapRecipients = (...recipientGroups) => {
  const recipientsByAddress = new Map();

  for (const group of recipientGroups) {
    for (const recipient of group) {
      const address = normalizeRecipientAddress(recipient.address);
      const der = canonicalizeRsaPublicKey(recipient.der, address);
      const existing = recipientsByAddress.get(address);
      if (existing) {
        if (!existing.der.equals(der)) {
          throw new Error(`Conflicting RSA public keys configured for recipient ${address}.`);
        }
        continue;
      }
      recipientsByAddress.set(address, { address, der });
    }
  }

  return [...recipientsByAddress.values()];
};
