import crypto from "node:crypto";
import Web3 from "web3";

const SERVICES = "services:";
const WORKER_SERVICE = "dfl-worker:";
const PARTICIPANT_KEY_VOLUME = "participant-key-state:";
const IMAGE_KEY = "image:";
const RESTART_KEY = "restart:";
const SHA256_MARKER = "@sha256:";
const YAML_MERGE_KEY = "<<:";
const ENVIRONMENT_KEY = "environment:";
const MAX_ENVIRONMENT_KEYS = 128;
const PLATFORM_PRE_LAUNCH_SHA256 =
    "bf12939bc82c9bdd103b6b1226913e6da58ed7cfcfc7c7ae808ac0813715b9a8";

const OUTER_MANIFEST_FIELDS = new Set([
    "docker_compose_file",
    "manifest_version",
    "runner",
    "name",
    "pre_launch_script",
    "allowed_envs",
    "features",
    "gateway_enabled",
    "kms_enabled",
    "local_key_provider_enabled",
    "no_instance_id",
    "public_logs",
    "public_sysinfo",
    "public_tcbinfo",
    "secure_time",
    "storage_fs",
    "tproxy_enabled",
]);

const SAFE_OUTER_ENVIRONMENT_KEYS = new Set([
    "PRIVATE_KEY",
    "SELLO_SERVICE_SIGNING_SEED",
    "SELLO_TOKEN_ISSUER_PUBLIC_KEY",
]);

const SAFE_OUTER_FEATURES = new Set(["kms", "tproxy-net"]);

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
] as const;

type FieldName = (typeof FIELD_NAMES)[number];

const FIELD_INDEX = new Map<FieldName, number>(
    FIELD_NAMES.map((name, index) => [name, index]),
);

const DSTACK_SOCKET_PATHS = [
    Buffer.from("/var/run/dstack.sock"),
    Buffer.from("/var/run/tappd.sock"),
];

const hash = (value: Buffer | string): Buffer => {
    const encoded = typeof value === "string" ? Buffer.from(value) : value;
    return Buffer.from(Web3.utils.keccak256(encoded).slice(2), "hex");
};

const word = (value: Buffer | bigint | number): Buffer => {
    if (Buffer.isBuffer(value)) {
        if (value.length !== 32) throw new Error("ABI word must be exactly 32 bytes");
        return value;
    }
    const integer = typeof value === "bigint" ? value : BigInt(value);
    if (integer < 0n || integer >= (1n << 256n)) {
        throw new Error("ABI integer is outside uint256");
    }
    return Buffer.from(integer.toString(16).padStart(64, "0"), "hex");
};

const abiHash = (...values: Array<Buffer | bigint | number>): Buffer =>
    hash(Buffer.concat(values.map(word)));

const domain = (value: string): Buffer => hash(value);

const DOCKER_WORKER_POLICY_DOMAIN = domain("MasterThesis.AppCompose.worker-policy.v1");
const WORKER_POLICY_DOMAIN = domain("MasterThesis.AppCompose.worker-policy.v2");
const OUTER_MANIFEST_POLICY_DOMAIN =
    domain("MasterThesis.AppCompose.outer-manifest-policy.v2");
const VOLUME_POLICY_DOMAIN = domain("volume-policy");
const CAPABILITIES_POLICY_DOMAIN = domain("capabilities-policy");
const SECURITY_OPTIONS_POLICY_DOMAIN = domain("security-options-policy");
const NETWORK_POLICY_DOMAIN = domain("network-policy");
const DSTACK_SOCKET_POLICY_DOMAIN = domain("dstack-socket-policy");
const ENVIRONMENT_POLICY_DOMAIN = domain("environment-safety-policy");
const VARIABLE_ENVIRONMENT_DOMAIN = domain("variable-environment-entry");
const FIXED_ENVIRONMENT_DOMAIN = domain("fixed-environment-entry");
const FIELD_DOMAINS = new Map<FieldName, Buffer>(
    FIELD_NAMES.map((name) => [name, domain(name)]),
);

type Accumulator = {
    chains: Buffer[];
    counts: number[];
    seen: Set<FieldName>;
    activeField?: FieldName;
    inEnvironment: boolean;
    dstackSocketChain: Buffer;
    dstackSocketCount: number;
    environmentXor: Buffer;
    environmentKeys: Set<string>;
    environmentSeen: boolean;
    restartSeen: boolean;
};

const zeroHash = () => Buffer.alloc(32);

const skipJsonWhitespace = (text: string, start: number): number => {
    let cursor = start;
    while (
        cursor < text.length
        && (
            text[cursor] === " " || text[cursor] === "\t"
            || text[cursor] === "\n" || text[cursor] === "\r"
        )
    ) cursor += 1;
    return cursor;
};

const scanJsonString = (text: string, start: number): [string, number] => {
    if (text[start] !== '"') throw new Error("expected JSON string");
    for (let cursor = start + 1; cursor < text.length; cursor += 1) {
        const current = text.charCodeAt(cursor);
        if (current === 0x22) {
            const encoded = text.slice(start, cursor + 1);
            return [JSON.parse(encoded) as string, cursor + 1];
        }
        if (current < 0x20) throw new Error("invalid JSON string");
        if (current !== 0x5c) continue;
        cursor += 1;
        if (cursor >= text.length || !'"\\/bfnrt'.includes(text[cursor])) {
            throw new Error("unsupported JSON escape");
        }
    }
    throw new Error("unterminated JSON string");
};

const scanJsonValue = (text: string, start: number): number => {
    if (text[start] === '"') return scanJsonString(text, start)[1];
    if (text[start] === "[" || text[start] === "{") {
        const stack: string[] = [text[start]];
        let cursor = start + 1;
        while (cursor < text.length && stack.length > 0) {
            if (text[cursor] === '"') {
                cursor = scanJsonString(text, cursor)[1];
                continue;
            }
            if (text[cursor] === "[" || text[cursor] === "{") {
                stack.push(text[cursor]);
            } else if (text[cursor] === "]" || text[cursor] === "}") {
                const open = stack.pop();
                if (
                    (open === "[" && text[cursor] !== "]")
                    || (open === "{" && text[cursor] !== "}")
                ) {
                    throw new Error("invalid nested JSON value");
                }
            }
            cursor += 1;
        }
        if (stack.length !== 0) throw new Error("unterminated JSON value");
        return cursor;
    }

    let cursor = start;
    while (cursor < text.length && text[cursor] !== "," && text[cursor] !== "}") {
        cursor += 1;
    }
    const token = text.slice(start, cursor).trim();
    if (!token) throw new Error("invalid outer app compose value");
    JSON.parse(token);
    return cursor;
};

const assertUniqueTopLevelFields = (text: string): void => {
    let cursor = skipJsonWhitespace(text, 0);
    if (text[cursor] !== "{") throw new Error("app compose must be a JSON object");
    cursor += 1;
    let first = true;
    const seen = new Set<string>();
    while (true) {
        cursor = skipJsonWhitespace(text, cursor);
        if (cursor >= text.length) throw new Error("unterminated app compose object");
        if (text[cursor] === "}") {
            cursor += 1;
            break;
        }
        if (!first) {
            if (text[cursor] !== ",") throw new Error("invalid app compose separator");
            cursor = skipJsonWhitespace(text, cursor + 1);
        }
        first = false;
        const [key, afterKey] = scanJsonString(text, cursor);
        if (seen.has(key)) throw new Error("outer app compose field duplicated");
        seen.add(key);
        cursor = skipJsonWhitespace(text, afterKey);
        if (text[cursor] !== ":") throw new Error("invalid app compose field");
        cursor = skipJsonWhitespace(text, cursor + 1);
        cursor = scanJsonValue(text, cursor);
        cursor = skipJsonWhitespace(text, cursor);
        if (text[cursor] !== "," && text[cursor] !== "}") {
            throw new Error("invalid outer app compose value");
        }
    }
    if (skipJsonWhitespace(text, cursor) !== text.length) {
        throw new Error("trailing app compose data");
    }
};

type OuterManifest = Record<string, unknown> & {
    docker_compose_file: string;
    manifest_version: number;
    runner: string;
};

const stringArray = (value: unknown, label: string): string[] => {
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
        throw new Error(`${label} must be an array`);
    }
    return value as string[];
};

const assertUniqueAllowedValues = (
    values: string[],
    allowed: Set<string>,
    unsafeMessage: string,
    duplicateMessage: string,
): void => {
    const seen = new Set<string>();
    for (const value of values) {
        if (!allowed.has(value)) throw new Error(unsafeMessage);
        if (seen.has(value)) throw new Error(duplicateMessage);
        seen.add(value);
    }
};

const parseOuterManifest = (canonicalAppCompose: Buffer): OuterManifest => {
    let text: string;
    try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(canonicalAppCompose);
    } catch {
        throw new Error("app compose is not valid UTF-8");
    }
    assertUniqueTopLevelFields(text);

    const parsed = JSON.parse(text) as unknown;
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("app compose must be a JSON object");
    }
    const manifest = parsed as Record<string, unknown>;
    for (const key of Object.keys(manifest)) {
        if (!OUTER_MANIFEST_FIELDS.has(key)) {
            throw new Error("unknown outer app compose field");
        }
    }
    if (
        !Object.hasOwn(manifest, "docker_compose_file")
        || !Object.hasOwn(manifest, "manifest_version")
        || !Object.hasOwn(manifest, "runner")
    ) {
        throw new Error("required outer app compose field missing");
    }
    if (
        typeof manifest.docker_compose_file !== "string"
        || manifest.docker_compose_file.length === 0
    ) {
        throw new Error("empty docker compose field");
    }
    if (manifest.manifest_version !== 2) {
        throw new Error("manifest_version must equal 2");
    }
    if (manifest.runner !== "docker-compose") {
        throw new Error("runner must be docker-compose");
    }
    if (
        Object.hasOwn(manifest, "name")
        && (
            typeof manifest.name !== "string"
            || Buffer.byteLength(manifest.name) > 256
        )
    ) {
        throw new Error("app compose name too long");
    }
    if (Object.hasOwn(manifest, "pre_launch_script")) {
        if (typeof manifest.pre_launch_script !== "string") {
            throw new Error("expected JSON string");
        }
        const digest = crypto
            .createHash("sha256")
            .update(Buffer.from(manifest.pre_launch_script))
            .digest("hex");
        if (manifest.pre_launch_script.length !== 0 && digest !== PLATFORM_PRE_LAUNCH_SHA256) {
            throw new Error("user pre-launch script not allowed");
        }
    }
    if (Object.hasOwn(manifest, "allowed_envs")) {
        assertUniqueAllowedValues(
            stringArray(manifest.allowed_envs, "allowed_envs"),
            SAFE_OUTER_ENVIRONMENT_KEYS,
            "unsafe outer environment key",
            "outer environment key duplicated",
        );
    }
    if (Object.hasOwn(manifest, "features")) {
        assertUniqueAllowedValues(
            stringArray(manifest.features, "features"),
            SAFE_OUTER_FEATURES,
            "unsupported outer app feature",
            "outer app feature duplicated",
        );
    }
    for (const key of ["kms_enabled", "tproxy_enabled"] as const) {
        if (Object.hasOwn(manifest, key) && manifest[key] !== true) {
            throw new Error("required outer feature disabled");
        }
    }
    for (const key of ["local_key_provider_enabled", "no_instance_id"] as const) {
        if (Object.hasOwn(manifest, key) && manifest[key] !== false) {
            throw new Error("unsafe outer app compose setting");
        }
    }
    for (
        const key of [
            "gateway_enabled",
            "public_logs",
            "public_sysinfo",
            "public_tcbinfo",
            "secure_time",
        ] as const
    ) {
        if (Object.hasOwn(manifest, key) && typeof manifest[key] !== "boolean") {
            throw new Error("invalid outer boolean");
        }
    }
    if (
        Object.hasOwn(manifest, "storage_fs")
        && manifest.storage_fs !== null
        && manifest.storage_fs !== "zfs"
        && manifest.storage_fs !== "ext4"
    ) {
        throw new Error("unsupported storage_fs");
    }
    return manifest as OuterManifest;
};

const trimPolicyRecord = (value: Buffer): Buffer => {
    let start = 0;
    let end = value.length;
    while (start < end && (value[start] === 0x20 || value[start] === 0x09)) start += 1;
    while (
        end > start
        && (value[end - 1] === 0x20 || value[end - 1] === 0x09 || value[end - 1] === 0x0d)
    ) {
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

const appendRecord = (
    accumulator: Accumulator,
    field: FieldName,
    relativeIndent: number,
    rawContent: Buffer,
) => {
    if (!Number.isSafeInteger(relativeIndent) || relativeIndent < 0 || relativeIndent > 0xffff) {
        throw new Error("worker policy indentation is outside uint16");
    }
    const content = trimPolicyRecord(rawContent);
    const record = abiHash(relativeIndent, hash(content));
    const index = FIELD_INDEX.get(field);
    if (index === undefined) throw new Error(`unknown worker policy field ${field}`);
    accumulator.chains[index] = abiHash(accumulator.chains[index], record);
    accumulator.counts[index] += 1;

    if (
        field === "volumes"
        && DSTACK_SOCKET_PATHS.some((socketPath) => content.indexOf(socketPath) !== -1)
    ) {
        accumulator.dstackSocketChain = abiHash(accumulator.dstackSocketChain, record);
        accumulator.dstackSocketCount += 1;
    }
};

const xorHash = (left: Buffer, right: Buffer): Buffer => {
    if (left.length !== 32 || right.length !== 32) {
        throw new Error("environment policy hashes must be 32 bytes");
    }
    return Buffer.from(left.map((value, index) => value ^ right[index]));
};

const appendEnvironmentRecord = (accumulator: Accumulator, rawContent: Buffer) => {
    const content = trimPolicyRecord(rawContent).toString("utf8");
    const separator = content.indexOf(":");
    if (separator <= 0) throw new Error("invalid worker environment entry");

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

const fieldHash = (accumulator: Accumulator, field: FieldName): Buffer => {
    const index = FIELD_INDEX.get(field);
    const fieldDomain = FIELD_DOMAINS.get(field);
    if (index === undefined || fieldDomain === undefined) {
        throw new Error(`unknown worker policy field ${field}`);
    }
    return abiHash(fieldDomain, accumulator.chains[index], accumulator.counts[index]);
};

const imageDigest = (imageReference: string): Buffer => {
    const markerOffset = imageReference.indexOf(SHA256_MARKER, 1);
    if (
        markerOffset < 1
        || imageReference.indexOf(SHA256_MARKER, markerOffset + 1) !== -1
    ) {
        throw new Error("worker image must be digest-pinned");
    }
    const digest = imageReference.slice(markerOffset + SHA256_MARKER.length);
    if (!/^[0-9a-f]{64}$/.test(digest) || /^0+$/.test(digest)) {
        throw new Error("invalid worker image digest");
    }
    return Buffer.from(digest, "hex");
};

const imageValue = (value: string): string => {
    let output = value.trim();
    if (!output) throw new Error("empty worker image");
    if (output.startsWith('"') || output.startsWith("'")) {
        if (!output.endsWith(output[0]) || output.length < 2) {
            throw new Error("invalid worker image quoting");
        }
        output = output.slice(1, -1);
    }
    if (!output || /[#\t ]/.test(output)) throw new Error("invalid worker image value");
    return output;
};

export type WorkerPolicyIdentity = {
    imageDigest: string;
    workerPolicyHash: string;
};

export const deriveWorkerPolicyIdentity = (
    canonicalAppCompose: Buffer | Uint8Array | string,
): WorkerPolicyIdentity => {
    const canonicalBytes = typeof canonicalAppCompose === "string"
        ? Buffer.from(canonicalAppCompose)
        : Buffer.from(canonicalAppCompose);
    if (canonicalBytes.length === 0 || canonicalBytes.length > 65_536) {
        throw new Error("invalid app compose size");
    }
    const manifest = parseOuterManifest(canonicalBytes);
    const compose = Buffer.from(manifest.docker_compose_file);
    const accumulator: Accumulator = {
        chains: FIELD_NAMES.map(zeroHash),
        counts: FIELD_NAMES.map(() => 0),
        seen: new Set<FieldName>(),
        inEnvironment: false,
        dstackSocketChain: zeroHash(),
        dstackSocketCount: 0,
        environmentXor: zeroHash(),
        environmentKeys: new Set<string>(),
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
    let workerImage: string | undefined;
    let imageCount = 0;

    for (const rawLine of compose.toString("utf8").split("\n")) {
        const withoutCarriageReturn = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
        const trailingTrimmed = withoutCarriageReturn.replace(/ +$/u, "");
        const indent = trailingTrimmed.length - trailingTrimmed.replace(/^ +/u, "").length;
        const content = trailingTrimmed.slice(indent);
        if (!content || content.startsWith("#")) continue;

        if (indent === 0) {
            const services = content === SERVICES;
            const volumes = content === "volumes:";
            if (!services && !volumes) throw new Error("unknown top-level compose field");
            if (services) {
                if (servicesSeen) throw new Error("services field duplicated");
                servicesSeen = true;
            }
            if (volumes) {
                if (topLevelVolumesSeen) throw new Error("top-level volumes field duplicated");
                topLevelVolumesSeen = true;
            }
            inServices = services;
            inTopLevelVolumes = volumes;
            inWorker = false;
            accumulator.activeField = undefined;
            accumulator.inEnvironment = false;
        } else if (inServices && indent === 2) {
            serviceCount += 1;
            inWorker = content === WORKER_SERVICE;
            if (inWorker) workerCount += 1;
            accumulator.activeField = undefined;
            accumulator.inEnvironment = false;
        } else if (inTopLevelVolumes && indent === 2) {
            topLevelVolumeCount += 1;
            if (content !== PARTICIPANT_KEY_VOLUME) {
                throw new Error("unsupported top-level volume");
            }
        } else if (inTopLevelVolumes && indent > 2) {
            throw new Error("top-level volume options not allowed");
        } else if (inWorker && indent === 4) {
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
                if (accumulator.restartSeen) throw new Error("worker restart field duplicated");
                accumulator.restartSeen = true;
                continue;
            }
            if (content.startsWith(ENVIRONMENT_KEY)) {
                if (accumulator.environmentSeen) throw new Error("worker policy field duplicated");
                accumulator.environmentSeen = true;
                if (content.slice(ENVIRONMENT_KEY.length).trim() !== "") {
                    throw new Error("worker environment must use mapping form");
                }
                accumulator.inEnvironment = true;
                continue;
            }
            const field = FIELD_NAMES.find((candidate) => content.startsWith(`${candidate}:`));
            if (field !== undefined) {
                if (accumulator.seen.has(field)) throw new Error("worker policy field duplicated");
                accumulator.seen.add(field);
                accumulator.activeField = field;
                appendRecord(accumulator, field, 0, Buffer.from(content.slice(field.length + 1)));
                continue;
            }
            throw new Error("unknown worker service field");
        } else if (inWorker && indent > 4 && accumulator.inEnvironment) {
            if (indent !== 6) throw new Error("invalid worker environment indentation");
            appendEnvironmentRecord(accumulator, Buffer.from(content));
        } else if (inWorker && indent > 4 && accumulator.activeField !== undefined) {
            appendRecord(
                accumulator,
                accumulator.activeField,
                indent - 4,
                Buffer.from(content),
            );
        } else if (inWorker && indent > 4) {
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
    const volumes = abiHash(
        VOLUME_POLICY_DOMAIN,
        fieldHash(accumulator, "volumes"),
        fieldHash(accumulator, "tmpfs"),
        fieldHash(accumulator, "devices"),
    );
    const capabilities = abiHash(
        CAPABILITIES_POLICY_DOMAIN,
        fieldHash(accumulator, "cap_add"),
        fieldHash(accumulator, "cap_drop"),
    );
    const securityOptions = abiHash(
        SECURITY_OPTIONS_POLICY_DOMAIN,
        fieldHash(accumulator, "security_opt"),
        fieldHash(accumulator, "read_only"),
        fieldHash(accumulator, "privileged"),
        abiHash(
            ENVIRONMENT_POLICY_DOMAIN,
            accumulator.environmentXor,
            accumulator.environmentKeys.size,
        ),
    );
    const networkPolicy = abiHash(
        NETWORK_POLICY_DOMAIN,
        fieldHash(accumulator, "network_mode"),
        fieldHash(accumulator, "networks"),
        fieldHash(accumulator, "ports"),
        fieldHash(accumulator, "expose"),
    );
    const dstackSocketPolicy = abiHash(
        DSTACK_SOCKET_POLICY_DOMAIN,
        accumulator.dstackSocketChain,
        accumulator.dstackSocketCount,
    );
    const dockerPolicyHash = abiHash(
        DOCKER_WORKER_POLICY_DOMAIN,
        digest,
        entrypoint,
        command,
        user,
        volumes,
        capabilities,
        securityOptions,
        networkPolicy,
        dstackSocketPolicy,
    );
    const outerManifestPolicyHash = abiHash(
        OUTER_MANIFEST_POLICY_DOMAIN,
        2,
        hash("docker-compose"),
        Buffer.from(PLATFORM_PRE_LAUNCH_SHA256, "hex"),
    );
    const policyHash = abiHash(
        WORKER_POLICY_DOMAIN,
        dockerPolicyHash,
        outerManifestPolicyHash,
    );

    return {
        imageDigest: `0x${digest.toString("hex")}`,
        workerPolicyHash: `0x${policyHash.toString("hex")}`,
    };
};
