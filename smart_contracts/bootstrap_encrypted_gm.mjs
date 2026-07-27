import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {
  canonicalizeRsaPublicKey,
  deriveRsaPublicKeyDer,
  normalizeRecipientAddress,
  waitForBootstrapRecipients,
} from "./bootstrap_recipients.mjs";

const args = process.argv.slice(2);
const readArg = (flag) => {
  const index = args.indexOf(flag);
  if (index === -1 || index + 1 >= args.length) {
    throw new Error(`Missing required argument ${flag}`);
  }
  return args[index + 1];
};

const modelPath = readArg("--model");
const signaturePath = readArg("--signature");
const privateKeyPath = readArg("--private-key");
const outDir = readArg("--out-dir");
const round = Number(readArg("--round"));
const rpcUrl = readArg("--rpc-url");
const registryAddress = String(readArg("--registry-address")).trim().toLowerCase();
const optionalArg = (flag) => {
  const index = args.indexOf(flag);
  if (index === -1 || index + 1 >= args.length) {
    return "";
  }
  return args[index + 1];
};
const readIntegerOption = (flag, fallback, minimum) => {
  const raw = optionalArg(flag);
  const value = raw === "" ? fallback : Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new Error(`${flag} must be an integer greater than or equal to ${minimum}.`);
  }
  return value;
};
const minimumRecipients = readIntegerOption("--minimum-recipients", 1, 1);
const registrationSettleSeconds = readIntegerOption("--registration-settle-seconds", 0, 0);
const registrationTimeoutSeconds = readIntegerOption("--registration-timeout-seconds", 900, 1);
const registrationPollSeconds = readIntegerOption("--registration-poll-seconds", 2, 1);
const requiredRecipientsFile = optionalArg("--required-recipients-file");

const loadRequiredRecipientAddresses = () => {
  if (!requiredRecipientsFile) {
    return null;
  }

  let declaration;
  try {
    declaration = JSON.parse(fs.readFileSync(requiredRecipientsFile, "utf8"));
  } catch (error) {
    throw new Error(`Could not read bootstrap recipient declaration: ${error.message}`);
  }
  if (!declaration || declaration.status !== "declared") {
    throw new Error("Bootstrap recipient declaration must have status=declared.");
  }
  if (!Array.isArray(declaration.recipients) || declaration.recipients.length === 0) {
    throw new Error("Bootstrap recipient declaration must contain a non-empty recipients array.");
  }
  return declaration.recipients.map((address, index) => (
    normalizeRecipientAddress(address, `declared recipient ${index}`)
  ));
};
const requiredRecipientAddresses = loadRequiredRecipientAddresses();

const normalizeHex = (value) => {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  return trimmed.startsWith("0x") || trimmed.startsWith("0X") ? trimmed.slice(2) : trimmed;
};

const ensureEvenHex = (value) => {
  const hex = normalizeHex(value);
  return hex.length % 2 === 0 ? hex : `0${hex}`;
};

const hexToBuffer = (value) => Buffer.from(ensureEvenHex(value), "hex");

const readWord = (buffer, wordIndex) => {
  const start = wordIndex * 32;
  return buffer.subarray(start, start + 32);
};

const wordToNumber = (word) => Number(BigInt(`0x${word.toString("hex")}`));

const decodeAddressArray = (resultHex) => {
  const buffer = hexToBuffer(resultHex);
  if (buffer.length < 64) {
    throw new Error("Invalid ABI payload for address[] response.");
  }

  const offset = wordToNumber(readWord(buffer, 0));
  const length = wordToNumber(buffer.subarray(offset, offset + 32));
  const valuesStart = offset + 32;
  const addresses = [];

  for (let index = 0; index < length; index++) {
    const wordStart = valuesStart + (index * 32);
    const word = buffer.subarray(wordStart, wordStart + 32);
    addresses.push(`0x${word.subarray(12).toString("hex")}`);
  }

  return addresses;
};

const decodePublicKeyBytes = (resultHex) => {
  const buffer = hexToBuffer(resultHex);
  if (buffer.length < 128) {
    throw new Error("Invalid ABI payload for getDevice response.");
  }

  const publicKeyOffset = wordToNumber(readWord(buffer, 3));
  if (publicKeyOffset + 32 > buffer.length) {
    throw new Error("Invalid public key offset in getDevice response.");
  }

  const keyLength = wordToNumber(buffer.subarray(publicKeyOffset, publicKeyOffset + 32));
  const keyStart = publicKeyOffset + 32;
  const keyEnd = keyStart + keyLength;
  if (keyEnd > buffer.length) {
    throw new Error("Invalid public key length in getDevice response.");
  }

  return buffer.subarray(keyStart, keyEnd);
};

const rpcCall = async (to, data) => {
  const response = await fetch(rpcUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "eth_call",
      params: [{ to, data }, "latest"],
    }),
  });

  if (!response.ok) {
    throw new Error(`RPC call failed with status ${response.status}.`);
  }

  const payload = await response.json();
  if (payload.error) {
    throw new Error(`RPC call failed: ${payload.error.message || JSON.stringify(payload.error)}`);
  }

  return String(payload.result || "0x");
};

const encodeAddressArg = (address) => {
  const hex = normalizeHex(address).toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(hex)) {
    throw new Error(`Invalid address: ${address}`);
  }
  return hex.padStart(64, "0");
};

const getAuthorizedDevices = async () => {
  const result = await rpcCall(registryAddress, "0x1426799f");
  return decodeAddressArray(result);
};

const getDevicePublicKey = async (address) => {
  const result = await rpcCall(registryAddress, `0x00d55318${encodeAddressArg(address)}`);
  return decodePublicKeyBytes(result);
};

const loadRecipientsFromRegistry = async () => {
  const recipients = [];
  const addresses = await getAuthorizedDevices();

  for (const address of addresses) {
    const publicKeyDer = await getDevicePublicKey(address);
    if (!publicKeyDer.length) {
      continue;
    }
    recipients.push({
      address: normalizeRecipientAddress(address, "on-chain registry recipient"),
      der: canonicalizeRsaPublicKey(publicKeyDer, `on-chain registry recipient ${address}`),
    });
  }

  return recipients;
};

let lastProgress = "";
const recipients = await waitForBootstrapRecipients(loadRecipientsFromRegistry, {
  minimumRecipients: requiredRecipientAddresses?.length ?? minimumRecipients,
  registrationSettleMs: registrationSettleSeconds * 1000,
  timeoutMs: registrationTimeoutSeconds * 1000,
  pollIntervalMs: registrationPollSeconds * 1000,
  requiredRecipientAddresses,
  onProgress: ({ recipientCount, minimumRecipients: targetCount, phase, remainingMs }) => {
    const progress = phase === "waiting" || phase === "waiting-required"
      ? `Waiting for live DeviceRegistry recipients: ${recipientCount}/${targetCount}.`
      : `DeviceRegistry threshold reached with ${recipientCount} recipient(s); `
        + `settling for ${Math.ceil(remainingMs / 1000)} more second(s).`;
    if (progress !== lastProgress) {
      console.error(progress);
      lastProgress = progress;
    }
  },
});
console.error(
  `Using ${recipients.length} live DeviceRegistry recipient(s) for encrypted bootstrap.`
);

const modelBytes = fs.readFileSync(modelPath);
const signatureBytes = fs.readFileSync(signaturePath);
const signingKeyPem = fs.readFileSync(privateKeyPath, "utf8");
const publisherPublicKeyDer = deriveRsaPublicKeyDer(
  signingKeyPem,
  "initial global-model signing key"
);
const signingKey = crypto.createPrivateKey(signingKeyPem);

fs.mkdirSync(outDir, { recursive: true });
const bundlePath = path.join(outDir, "aggregated.bundle.enc");
const bundleSignaturePath = path.join(outDir, "aggregated.bundle.enc.sig");
const keyBundlePath = path.join(outDir, "aggregated.bundle.keys.json");

const payload = Buffer.from(JSON.stringify({
  version: 1,
  round,
  generated_at: new Date().toISOString(),
  model_b64: modelBytes.toString("base64"),
  signature_b64: signatureBytes.toString("base64"),
}), "utf8");

const aesKey = crypto.randomBytes(32);
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv("aes-256-gcm", aesKey, iv);
const ciphertext = Buffer.concat([cipher.update(payload), cipher.final()]);
const authTag = cipher.getAuthTag();

const wrappedKeys = {};
if (recipients.length === 0) {
  throw new Error("Encrypted bootstrap requires at least one live DeviceRegistry recipient.");
}
for (const recipient of recipients) {
  const publicKey = crypto.createPublicKey({
    key: recipient.der,
    format: "der",
    type: "spki",
  });
  const wrapped = crypto.publicEncrypt({
    key: publicKey,
    padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
    oaepHash: "sha256",
  }, Buffer.concat([aesKey, iv]));
  wrappedKeys[recipient.address] = wrapped.toString("base64");
}

const bundleDocument = {
  version: 1,
  type: "global-model-bundle",
  cipher: "aes-256-gcm",
  iv_b64: iv.toString("base64"),
  auth_tag_b64: authTag.toString("base64"),
  ciphertext_b64: ciphertext.toString("base64"),
};
fs.writeFileSync(bundlePath, JSON.stringify(bundleDocument));

const bundleSignature = crypto.sign("RSA-SHA256", fs.readFileSync(bundlePath), {
  key: signingKey,
  padding: crypto.constants.RSA_PKCS1_PADDING,
});
fs.writeFileSync(bundleSignaturePath, bundleSignature);

const keyBundleDocument = {
  version: 1,
  type: "global-model-key-bundle",
  round,
  wrapped_keys_b64: wrappedKeys,
};
fs.writeFileSync(keyBundlePath, JSON.stringify(keyBundleDocument));

process.stdout.write(JSON.stringify({
  bundlePath,
  bundleSignaturePath,
  keyBundlePath,
  recipientCount: recipients.length,
  recipients: recipients.map((recipient) => recipient.address),
  publisherPublicKeyDerHex: publisherPublicKeyDer.toString("hex"),
}) + "\n");
