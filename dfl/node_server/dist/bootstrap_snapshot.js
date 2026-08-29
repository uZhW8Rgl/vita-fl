import crypto from "crypto";
const normalizeAddress = (value, label) => {
    const normalized = String(value || "").trim().toLowerCase();
    if (!/^0x[0-9a-f]{40}$/.test(normalized)) {
        throw new Error(`${label} is not a valid Ethereum address.`);
    }
    return normalized;
};
export const normalizeRunRosterDigest = (value, label = "committed run-roster digest") => {
    const normalized = String(value || "").trim().toLowerCase();
    if (!/^0x[0-9a-f]{64}$/.test(normalized)) {
        throw new Error(`${label} is not a bytes32 digest.`);
    }
    return normalized;
};
export const normalizeBootstrapPublicKey = (value, label = "bootstrap recipient RSA public key") => {
    const raw = String(value || "").trim().replace(/^0x/i, "").toLowerCase();
    if (!raw || raw.length % 2 !== 0 || !/^[0-9a-f]+$/.test(raw)) {
        throw new Error(`${label} is not non-empty DER hex.`);
    }
    let publicKey;
    try {
        publicKey = crypto.createPublicKey({
            key: Buffer.from(raw, "hex"),
            format: "der",
            type: "spki",
        });
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`${label} is not a valid DER/SPKI key: ${message}`);
    }
    if (publicKey.asymmetricKeyType !== "rsa") {
        throw new Error(`${label} is not an RSA key.`);
    }
    const canonicalDer = publicKey
        .export({ format: "der", type: "spki" })
        .toString("hex");
    if (canonicalDer !== raw) {
        throw new Error(`${label} is not canonical DER/SPKI encoding.`);
    }
    return `0x${canonicalDer}`;
};
const normalizeRecipientAddresses = (values) => {
    if (!Array.isArray(values) || values.length === 0) {
        throw new Error("Committed run roster requires at least one recipient address.");
    }
    const addresses = values.map((value, index) => normalizeAddress(value, `committed run-roster recipient ${index}`));
    if (new Set(addresses).size !== addresses.length) {
        throw new Error("Committed run-roster recipient addresses must be unique.");
    }
    return addresses;
};
export const createCommittedRunRosterBinding = ({ chainId, registryAddress, rosterDigest, recipientAddresses, }) => {
    if (!Number.isSafeInteger(chainId) || chainId <= 0) {
        throw new Error("Committed run-roster chain ID must be a positive integer.");
    }
    const recipients = normalizeRecipientAddresses(recipientAddresses);
    return {
        chainId,
        registryAddress: normalizeAddress(registryAddress, "run-roster registry"),
        rosterDigest: normalizeRunRosterDigest(rosterDigest),
        bootstrapWorker: recipients[0],
        workerCount: recipients.length,
        recipientAddresses: recipients,
    };
};
const normalizeFrozenRecipients = (values) => {
    if (!Array.isArray(values) || values.length === 0) {
        throw new Error("Round-0 bootstrap snapshot has no frozen recipients.");
    }
    const recipients = values.map((value, index) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
            throw new Error(`Round-0 bootstrap recipient ${index} is invalid.`);
        }
        const entry = value;
        return {
            address: normalizeAddress(entry.address, `round-0 bootstrap recipient ${index}`),
            publicKeyDerHex: normalizeBootstrapPublicKey(entry.publicKeyDerHex ?? entry.public_key_der_hex, `round-0 bootstrap recipient ${index} RSA public key`),
        };
    });
    if (new Set(recipients.map(({ address }) => address)).size !== recipients.length) {
        throw new Error("Round-0 bootstrap snapshot contains duplicate recipients.");
    }
    return recipients;
};
export const buildRoundZeroBootstrapSnapshot = (binding, recipientEntries) => {
    const recipients = normalizeFrozenRecipients(recipientEntries);
    const addresses = recipients.map(({ address }) => address);
    if (JSON.stringify(addresses) !== JSON.stringify(binding.recipientAddresses)) {
        throw new Error("Frozen round-0 recipient entries do not match the committed run roster.");
    }
    return {
        version: 3,
        chain_id: binding.chainId,
        registry_address: binding.registryAddress,
        run_roster_digest: binding.rosterDigest,
        bootstrap_worker: binding.bootstrapWorker,
        worker_count: binding.workerCount,
        recipients: recipients.map(({ address, publicKeyDerHex }) => ({
            address,
            public_key_der_hex: publicKeyDerHex,
        })),
    };
};
export const parseRoundZeroBootstrapSnapshot = (value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("Round-0 bootstrap snapshot is not a JSON object.");
    }
    const snapshot = value;
    if (snapshot.version !== 3) {
        throw new Error("Round-0 bootstrap snapshot is not version 3 bound to the committed run roster.");
    }
    const recipients = normalizeFrozenRecipients(snapshot.recipients);
    const chainId = Number(snapshot.chain_id);
    const workerCount = Number(snapshot.worker_count);
    if (!Number.isSafeInteger(chainId) || chainId <= 0) {
        throw new Error("Round-0 bootstrap snapshot has an invalid chain ID.");
    }
    if (!Number.isSafeInteger(workerCount) || workerCount !== recipients.length) {
        throw new Error("Round-0 bootstrap snapshot has an invalid worker count.");
    }
    const bootstrapWorker = normalizeAddress(snapshot.bootstrap_worker, "round-0 bootstrap snapshot worker");
    if (recipients[0].address !== bootstrapWorker) {
        throw new Error("Round-0 bootstrap snapshot worker is not the first recipient.");
    }
    return {
        version: 3,
        chain_id: chainId,
        registry_address: normalizeAddress(snapshot.registry_address, "round-0 bootstrap snapshot registry"),
        run_roster_digest: normalizeRunRosterDigest(snapshot.run_roster_digest, "round-0 bootstrap snapshot run-roster digest"),
        bootstrap_worker: bootstrapWorker,
        worker_count: workerCount,
        recipients: recipients.map(({ address, publicKeyDerHex }) => ({
            address,
            public_key_der_hex: publicKeyDerHex,
        })),
    };
};
export const frozenRecipientsForCommittedRoster = (snapshotValue, binding) => {
    const snapshot = parseRoundZeroBootstrapSnapshot(snapshotValue);
    const expectedContext = {
        chain_id: binding.chainId,
        registry_address: binding.registryAddress,
        run_roster_digest: binding.rosterDigest,
        bootstrap_worker: binding.bootstrapWorker,
        worker_count: binding.workerCount,
    };
    for (const [field, expected] of Object.entries(expectedContext)) {
        if (snapshot[field] !== expected) {
            throw new Error(`Round-0 bootstrap snapshot does not match the committed ${field}.`);
        }
    }
    const recipients = snapshot.recipients.map((entry) => ({
        address: entry.address,
        publicKeyDerHex: entry.public_key_der_hex,
    }));
    if (JSON.stringify(recipients.map(({ address }) => address))
        !== JSON.stringify(binding.recipientAddresses)) {
        throw new Error("Round-0 bootstrap snapshot recipients do not match the committed run roster.");
    }
    return recipients;
};
export const requireFrozenRecipientKeysMatchRegistry = (frozenValues, registeredValues) => {
    const frozen = normalizeFrozenRecipients(frozenValues);
    const registered = normalizeFrozenRecipients(registeredValues);
    if (frozen.length !== registered.length) {
        throw new Error("Frozen round-0 recipient keys do not match the immutable DeviceRegistry keys.");
    }
    for (let index = 0; index < frozen.length; index++) {
        if (frozen[index].address !== registered[index].address
            || frozen[index].publicKeyDerHex !== registered[index].publicKeyDerHex) {
            throw new Error(`Frozen round-0 recipient ${index} key does not match the `
                + "immutable DeviceRegistry registration.");
        }
    }
    return frozen;
};
