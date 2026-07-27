import Web3 from "web3";
const DOCKER_COMPOSE_KEY = Buffer.from('"docker_compose_file":"');
const SERVICES = "services:";
const WORKER_SERVICE = "dfl-worker:";
const PARTICIPANT_KEY_VOLUME = "participant-key-state:";
const IMAGE_KEY = "image:";
const RESTART_KEY = "restart:";
const SHA256_MARKER = "@sha256:";
const YAML_MERGE_KEY = "<<:";
const ENVIRONMENT_KEY = "environment:";
const MAX_ENVIRONMENT_KEYS = 128;
const VARIABLE_ENVIRONMENT_KEYS = new Set([
    "ACCOUNT_ADDRESS",
    "DEVICE_ID",
    "PUBLIC_IP",
    "MSG_BROKER_IP",
    "PRIVATE_KEY",
    "KUBO_API",
    "KUBO_GATEWAY",
    "DSTACK_GATEWAY_DOMAIN",
    "RPC_URL",
    "EXPECTED_RUNTIME_RPC_URL",
    "REGISTRY_ADDRESS",
    "AGGREGATOR_ADDRESS",
    "GM_STORAGE_ADDRESS",
    "MEDICAL_SIGNER_REGISTRY_ADDRESS",
    "DFL_TELEMETRY_URL",
    "SCITT_URL",
    "CLIENT_LIMIT",
    "EPOCH",
    "ROUND",
    "MODEL_SUBMISSION_DEADLINE_MS",
    "GM_UPDATE_TIMEOUT_MS",
    "GM_UPDATE_TIMEOUT_LOOPS",
    "AGGREGATION_UPDATE_ESTIMATE_MS",
    "GM_UPDATE_POLL_MS",
    "WORKER_COUNT",
    "DATASET_NAME",
    "TRAIN_IMAGES_SRC",
    "TRAIN_LABELS_SRC",
    "TEST_IMAGES_SRC",
    "TEST_LABELS_SRC",
    "TRAIN_DATA_SRC",
    "TEST_DATA_SRC",
    "SELLO_SERVICE_SIGNING_SEED",
    "SELLO_TOKEN_ISSUER_PUBLIC_KEY",
]);
const FIELD_NAMES = [
    "entrypoint",
    "command",
    "user",
    "volumes",
    "tmpfs",
    "devices",
    "cap_add",
    "cap_drop",
    "security_opt",
    "read_only",
    "privileged",
    "network_mode",
    "networks",
    "ports",
    "expose",
];
const FIELD_INDEX = new Map(FIELD_NAMES.map((name, index) => [name, index]));
const DSTACK_SOCKET_PATHS = [
    Buffer.from("/var/run/dstack.sock"),
    Buffer.from("/var/run/tappd.sock"),
];
const hash = (value) => {
    const encoded = typeof value === "string" ? Buffer.from(value) : value;
    return Buffer.from(Web3.utils.keccak256(encoded).slice(2), "hex");
};
const word = (value) => {
    if (Buffer.isBuffer(value)) {
        if (value.length !== 32)
            throw new Error("ABI word must be exactly 32 bytes");
        return value;
    }
    const integer = typeof value === "bigint" ? value : BigInt(value);
    if (integer < 0n || integer >= (1n << 256n)) {
        throw new Error("ABI integer is outside uint256");
    }
    return Buffer.from(integer.toString(16).padStart(64, "0"), "hex");
};
const abiHash = (...values) => hash(Buffer.concat(values.map(word)));
const domain = (value) => hash(value);
const WORKER_POLICY_DOMAIN = domain("MasterThesis.AppCompose.worker-policy.v1");
const VOLUME_POLICY_DOMAIN = domain("volume-policy");
const CAPABILITIES_POLICY_DOMAIN = domain("capabilities-policy");
const SECURITY_OPTIONS_POLICY_DOMAIN = domain("security-options-policy");
const NETWORK_POLICY_DOMAIN = domain("network-policy");
const DSTACK_SOCKET_POLICY_DOMAIN = domain("dstack-socket-policy");
const ENVIRONMENT_POLICY_DOMAIN = domain("environment-safety-policy");
const VARIABLE_ENVIRONMENT_DOMAIN = domain("variable-environment-entry");
const FIXED_ENVIRONMENT_DOMAIN = domain("fixed-environment-entry");
const FIELD_DOMAINS = new Map(FIELD_NAMES.map((name) => [name, domain(name)]));
const zeroHash = () => Buffer.alloc(32);
const extractDockerCompose = (canonicalAppCompose) => {
    const offsets = [];
    let cursor = 0;
    while (cursor + DOCKER_COMPOSE_KEY.length <= canonicalAppCompose.length) {
        const found = canonicalAppCompose.indexOf(DOCKER_COMPOSE_KEY, cursor);
        if (found === -1)
            break;
        offsets.push(found + DOCKER_COMPOSE_KEY.length);
        cursor = found + 1;
    }
    if (offsets.length !== 1) {
        throw new Error("docker compose field missing or ambiguous");
    }
    const output = [];
    let terminated = false;
    for (let index = offsets[0]; index < canonicalAppCompose.length; index += 1) {
        const current = canonicalAppCompose[index];
        if (current === 0x22) {
            terminated = true;
            break;
        }
        if (current !== 0x5c) {
            output.push(current);
            continue;
        }
        index += 1;
        if (index >= canonicalAppCompose.length)
            throw new Error("invalid JSON escape");
        const escaped = canonicalAppCompose[index];
        if (escaped === 0x22 || escaped === 0x5c || escaped === 0x2f)
            output.push(escaped);
        else if (escaped === 0x62)
            output.push(0x08);
        else if (escaped === 0x66)
            output.push(0x0c);
        else if (escaped === 0x6e)
            output.push(0x0a);
        else if (escaped === 0x72)
            output.push(0x0d);
        else if (escaped === 0x74)
            output.push(0x09);
        else
            throw new Error("unsupported JSON escape");
    }
    if (!terminated)
        throw new Error("unterminated docker compose field");
    return Buffer.from(output);
};
const trimPolicyRecord = (value) => {
    let start = 0;
    let end = value.length;
    while (start < end && (value[start] === 0x20 || value[start] === 0x09))
        start += 1;
    while (end > start
        && (value[end - 1] === 0x20 || value[end - 1] === 0x09 || value[end - 1] === 0x0d)) {
        end -= 1;
    }
    const output = value.subarray(start, end);
    for (const current of output) {
        if (current === 0x23 || current === 0x26 || current === 0x2a || current === 0x09) {
            throw new Error("unsupported worker policy YAML");
        }
    }
    return output;
};
const appendRecord = (accumulator, field, relativeIndent, rawContent) => {
    if (!Number.isSafeInteger(relativeIndent) || relativeIndent < 0 || relativeIndent > 0xffff) {
        throw new Error("worker policy indentation is outside uint16");
    }
    const content = trimPolicyRecord(rawContent);
    const record = abiHash(relativeIndent, hash(content));
    const index = FIELD_INDEX.get(field);
    if (index === undefined)
        throw new Error(`unknown worker policy field ${field}`);
    accumulator.chains[index] = abiHash(accumulator.chains[index], record);
    accumulator.counts[index] += 1;
    if (field === "volumes"
        && DSTACK_SOCKET_PATHS.some((socketPath) => content.indexOf(socketPath) !== -1)) {
        accumulator.dstackSocketChain = abiHash(accumulator.dstackSocketChain, record);
        accumulator.dstackSocketCount += 1;
    }
};
const xorHash = (left, right) => {
    if (left.length !== 32 || right.length !== 32) {
        throw new Error("environment policy hashes must be 32 bytes");
    }
    return Buffer.from(left.map((value, index) => value ^ right[index]));
};
const appendEnvironmentRecord = (accumulator, rawContent) => {
    const content = trimPolicyRecord(rawContent).toString("utf8");
    const separator = content.indexOf(":");
    if (separator <= 0)
        throw new Error("invalid worker environment entry");
    const key = content.slice(0, separator);
    if (!/^[A-Z_][A-Z0-9_]*$/u.test(key)) {
        throw new Error("invalid worker environment key");
    }
    if (accumulator.environmentKeys.has(key)) {
        throw new Error("worker environment key duplicated");
    }
    if (accumulator.environmentKeys.size >= MAX_ENVIRONMENT_KEYS) {
        throw new Error("too many worker environment entries");
    }
    accumulator.environmentKeys.add(key);
    const value = content.slice(separator + 1).replace(/^ +/u, "");
    const keyHash = hash(key);
    const entryHash = VARIABLE_ENVIRONMENT_KEYS.has(key)
        ? abiHash(VARIABLE_ENVIRONMENT_DOMAIN, keyHash)
        : abiHash(FIXED_ENVIRONMENT_DOMAIN, keyHash, hash(value));
    accumulator.environmentXor = xorHash(accumulator.environmentXor, entryHash);
};
const fieldHash = (accumulator, field) => {
    const index = FIELD_INDEX.get(field);
    const fieldDomain = FIELD_DOMAINS.get(field);
    if (index === undefined || fieldDomain === undefined) {
        throw new Error(`unknown worker policy field ${field}`);
    }
    return abiHash(fieldDomain, accumulator.chains[index], accumulator.counts[index]);
};
const imageDigest = (imageReference) => {
    const markerOffset = imageReference.indexOf(SHA256_MARKER, 1);
    if (markerOffset < 1
        || imageReference.indexOf(SHA256_MARKER, markerOffset + 1) !== -1) {
        throw new Error("worker image must be digest-pinned");
    }
    const digest = imageReference.slice(markerOffset + SHA256_MARKER.length);
    if (!/^[0-9a-f]{64}$/.test(digest) || /^0+$/.test(digest)) {
        throw new Error("invalid worker image digest");
    }
    return Buffer.from(digest, "hex");
};
const imageValue = (value) => {
    let output = value.trim();
    if (!output)
        throw new Error("empty worker image");
    if (output.startsWith('"') || output.startsWith("'")) {
        if (!output.endsWith(output[0]) || output.length < 2) {
            throw new Error("invalid worker image quoting");
        }
        output = output.slice(1, -1);
    }
    if (!output || /[#\t ]/.test(output))
        throw new Error("invalid worker image value");
    return output;
};
export const deriveWorkerPolicyIdentity = (canonicalAppCompose) => {
    const canonicalBytes = typeof canonicalAppCompose === "string"
        ? Buffer.from(canonicalAppCompose)
        : Buffer.from(canonicalAppCompose);
    if (canonicalBytes.length === 0 || canonicalBytes.length > 65_536) {
        throw new Error("invalid app compose size");
    }
    const compose = extractDockerCompose(canonicalBytes);
    const accumulator = {
        chains: FIELD_NAMES.map(zeroHash),
        counts: FIELD_NAMES.map(() => 0),
        seen: new Set(),
        inEnvironment: false,
        dstackSocketChain: zeroHash(),
        dstackSocketCount: 0,
        environmentXor: zeroHash(),
        environmentKeys: new Set(),
        environmentSeen: false,
        restartSeen: false,
    };
    let inServices = false;
    let inWorker = false;
    let servicesSeen = false;
    let inTopLevelVolumes = false;
    let topLevelVolumesSeen = false;
    let serviceCount = 0;
    let workerCount = 0;
    let topLevelVolumeCount = 0;
    let workerImage;
    let imageCount = 0;
    for (const rawLine of compose.toString("utf8").split("\n")) {
        const withoutCarriageReturn = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
        const trailingTrimmed = withoutCarriageReturn.replace(/ +$/u, "");
        const indent = trailingTrimmed.length - trailingTrimmed.replace(/^ +/u, "").length;
        const content = trailingTrimmed.slice(indent);
        if (!content || content.startsWith("#"))
            continue;
        if (indent === 0) {
            const services = content === SERVICES;
            const volumes = content === "volumes:";
            if (!services && !volumes)
                throw new Error("unknown top-level compose field");
            if (services) {
                if (servicesSeen)
                    throw new Error("services field duplicated");
                servicesSeen = true;
            }
            if (volumes) {
                if (topLevelVolumesSeen)
                    throw new Error("top-level volumes field duplicated");
                topLevelVolumesSeen = true;
            }
            inServices = services;
            inTopLevelVolumes = volumes;
            inWorker = false;
            accumulator.activeField = undefined;
            accumulator.inEnvironment = false;
        }
        else if (inServices && indent === 2) {
            serviceCount += 1;
            inWorker = content === WORKER_SERVICE;
            if (inWorker)
                workerCount += 1;
            accumulator.activeField = undefined;
            accumulator.inEnvironment = false;
        }
        else if (inTopLevelVolumes && indent === 2) {
            topLevelVolumeCount += 1;
            if (content !== PARTICIPANT_KEY_VOLUME) {
                throw new Error("unsupported top-level volume");
            }
        }
        else if (inTopLevelVolumes && indent > 2) {
            throw new Error("top-level volume options not allowed");
        }
        else if (inWorker && indent === 4) {
            accumulator.activeField = undefined;
            accumulator.inEnvironment = false;
            if (content.startsWith(YAML_MERGE_KEY)) {
                throw new Error("worker YAML merge not allowed");
            }
            if (content.startsWith(IMAGE_KEY)) {
                imageCount += 1;
                workerImage = imageValue(content.slice(IMAGE_KEY.length));
                continue;
            }
            if (content.startsWith(RESTART_KEY)) {
                if (accumulator.restartSeen)
                    throw new Error("worker restart field duplicated");
                accumulator.restartSeen = true;
                continue;
            }
            if (content.startsWith(ENVIRONMENT_KEY)) {
                if (accumulator.environmentSeen)
                    throw new Error("worker policy field duplicated");
                accumulator.environmentSeen = true;
                if (content.slice(ENVIRONMENT_KEY.length).trim() !== "") {
                    throw new Error("worker environment must use mapping form");
                }
                accumulator.inEnvironment = true;
                continue;
            }
            const field = FIELD_NAMES.find((candidate) => content.startsWith(`${candidate}:`));
            if (field !== undefined) {
                if (accumulator.seen.has(field))
                    throw new Error("worker policy field duplicated");
                accumulator.seen.add(field);
                accumulator.activeField = field;
                appendRecord(accumulator, field, 0, Buffer.from(content.slice(field.length + 1)));
                continue;
            }
            throw new Error("unknown worker service field");
        }
        else if (inWorker && indent > 4 && accumulator.inEnvironment) {
            if (indent !== 6)
                throw new Error("invalid worker environment indentation");
            appendEnvironmentRecord(accumulator, Buffer.from(content));
        }
        else if (inWorker && indent > 4 && accumulator.activeField !== undefined) {
            appendRecord(accumulator, accumulator.activeField, indent - 4, Buffer.from(content));
        }
        else if (inWorker && indent > 4) {
            throw new Error("unexpected worker service nesting");
        }
    }
    if (!servicesSeen || serviceCount !== 1 || workerCount !== 1) {
        throw new Error("worker service missing or ambiguous");
    }
    if (topLevelVolumesSeen && topLevelVolumeCount !== 1) {
        throw new Error("invalid top-level volumes");
    }
    if (imageCount !== 1 || workerImage === undefined) {
        throw new Error("worker image missing or ambiguous");
    }
    const digest = imageDigest(workerImage);
    const entrypoint = fieldHash(accumulator, "entrypoint");
    const command = fieldHash(accumulator, "command");
    const user = fieldHash(accumulator, "user");
    const volumes = abiHash(VOLUME_POLICY_DOMAIN, fieldHash(accumulator, "volumes"), fieldHash(accumulator, "tmpfs"), fieldHash(accumulator, "devices"));
    const capabilities = abiHash(CAPABILITIES_POLICY_DOMAIN, fieldHash(accumulator, "cap_add"), fieldHash(accumulator, "cap_drop"));
    const securityOptions = abiHash(SECURITY_OPTIONS_POLICY_DOMAIN, fieldHash(accumulator, "security_opt"), fieldHash(accumulator, "read_only"), fieldHash(accumulator, "privileged"), abiHash(ENVIRONMENT_POLICY_DOMAIN, accumulator.environmentXor, accumulator.environmentKeys.size));
    const networkPolicy = abiHash(NETWORK_POLICY_DOMAIN, fieldHash(accumulator, "network_mode"), fieldHash(accumulator, "networks"), fieldHash(accumulator, "ports"), fieldHash(accumulator, "expose"));
    const dstackSocketPolicy = abiHash(DSTACK_SOCKET_POLICY_DOMAIN, accumulator.dstackSocketChain, accumulator.dstackSocketCount);
    const policyHash = abiHash(WORKER_POLICY_DOMAIN, digest, entrypoint, command, user, volumes, capabilities, securityOptions, networkPolicy, dstackSocketPolicy);
    return {
        imageDigest: `0x${digest.toString("hex")}`,
        workerPolicyHash: `0x${policyHash.toString("hex")}`,
    };
};
