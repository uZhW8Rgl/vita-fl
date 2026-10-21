/** Stable keys ONLY for apps protected by our immutable, shared-policy AppAuth.
 * The local guard is defense in depth. Secret isolation relies on the KMS
 * rejecting unauthorized boot measurements before provisioning application keys.
 */
import crypto from "crypto";
import fs from "fs/promises";
import { constants } from "fs";
import path from "path";
import { DstackClient } from "@phala/dstack-sdk";
import { Web3 } from "web3";
import { requireVolatileDirectory, RUNTIME_KEY_DIRECTORY } from "./runtime_keys.js";
export const KMS_KEY_PATH = "vita-fl/kms-policy/participant-state/v1";
export const KMS_STATE_PATH = "/var/lib/vita-fl/participant-keys.v2.sealed.json";
export const ACTION_RUNTIME_KEY = "action-root.seed";
const binding = (context) => Buffer.from(JSON.stringify({
    domain: "VITA-FL:KMS-participant-state:v2", ...context,
}));
export const sealKeyBundle = (bundle, root, context) => {
    if (root.length !== 32)
        throw new Error("KMS root must contain 32 bytes");
    const key = Buffer.from(crypto.hkdfSync("sha256", root, Buffer.alloc(0), binding(context), 32));
    const plaintext = Buffer.from(JSON.stringify(bundle));
    try {
        const iv = crypto.randomBytes(12);
        const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
        cipher.setAAD(binding(context));
        const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
        return { version: 2, context, iv: iv.toString("hex"), tag: cipher.getAuthTag().toString("hex"), ciphertext: ciphertext.toString("base64") };
    }
    finally {
        key.fill(0);
        plaintext.fill(0);
    }
};
export const unsealKeyBundle = (sealed, root, context) => {
    if (root.length !== 32 || sealed.version !== 2 || !binding(context).equals(binding(sealed.context))) {
        throw new Error("Sealed participant context mismatch");
    }
    const key = Buffer.from(crypto.hkdfSync("sha256", root, Buffer.alloc(0), binding(context), 32));
    let plaintext;
    try {
        const decipher = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(sealed.iv, "hex"));
        decipher.setAAD(binding(context));
        decipher.setAuthTag(Buffer.from(sealed.tag, "hex"));
        plaintext = Buffer.concat([decipher.update(Buffer.from(sealed.ciphertext, "base64")), decipher.final()]);
        const bundle = JSON.parse(plaintext.toString("utf8"));
        const rsa = crypto.createPrivateKey(bundle.rsa);
        if (rsa.asymmetricKeyType !== "rsa" || Number(rsa.asymmetricKeyDetails?.modulusLength) < 3072)
            throw new Error("Invalid sealed RSA key");
        for (const name of ["action", "sello", "air"]) {
            if (!/^[a-f0-9]{64}$/.test(bundle[name]))
                throw new Error("Invalid sealed signing seed");
        }
        return bundle;
    }
    finally {
        key.fill(0);
        plaintext?.fill(0);
    }
};
export const generateBundle = async () => {
    const { privateKey } = await new Promise((resolve, reject) => {
        crypto.generateKeyPair("rsa", { modulusLength: 3072 }, (error, _publicKey, privateKey) => error ? reject(error) : resolve({ privateKey }));
    });
    return { rsa: privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
        action: crypto.randomBytes(32).toString("hex"), sello: crypto.randomBytes(32).toString("hex"), air: crypto.randomBytes(32).toString("hex") };
};
/** Corruption, wrong context, and inaccessible state NEVER silently replace an identity. */
export const loadOrCreateKmsBundle = async (filename, root, context, generate = generateBundle) => {
    const read = async () => {
        const handle = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
        try {
            const info = await handle.stat();
            if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) !== 0)
                throw new Error("Unsafe sealed-key file permissions");
            return unsealKeyBundle(JSON.parse(await handle.readFile("utf8")), root, context);
        }
        finally {
            await handle.close();
        }
    };
    try {
        return await read();
    }
    catch (error) {
        if (error.code !== "ENOENT")
            throw error;
    }
    await fs.mkdir(path.dirname(filename), { recursive: true, mode: 0o700 });
    const bundle = await generate();
    const temporary = `${filename}.${crypto.randomBytes(12).toString("hex")}.tmp`;
    const handle = await fs.open(temporary, "wx", 0o600);
    try {
        await handle.writeFile(JSON.stringify(sealKeyBundle(bundle, root, context)));
        await handle.sync();
        await handle.close();
        try {
            await fs.link(temporary, filename);
        }
        catch (error) {
            if (error.code !== "EEXIST")
                throw error;
        }
        // If another boot won creation, consume its authenticated identity.
        return await read();
    }
    finally {
        await handle.close().catch(() => { });
        await fs.rm(temporary, { force: true });
    }
};
export const attestKmsPolicyContext = async (env = process.env) => {
    if (env.DOCKER !== "phala" || env.PARTICIPANT_KEY_PROVIDER !== "kms")
        throw new Error("Explicit Phala KMS key provider required");
    const client = new DstackClient("/var/run/dstack.sock");
    const info = await client.info();
    const web3 = new Web3(env.RPC_URL);
    const context = {
        appId: `0x${info.app_id.replace(/^(0x|app_)/, "")}`.toLowerCase(),
        composeHash: `0x${crypto.createHash("sha256").update(info.tcb_info.app_compose).digest("hex")}`,
        participant: String(env.ACCOUNT_ADDRESS || "").toLowerCase(),
        registry: String(env.REGISTRY_ADDRESS || env.EXPECTED_DEVICE_REGISTRY_ADDRESS || "").toLowerCase(),
        chainId: String(env.EXPECTED_CHAIN_ID || ""),
    };
    for (const address of [context.appId, context.participant, context.registry]) {
        if (!/^0x[a-f0-9]{40}$/.test(address))
            throw new Error("Invalid KMS context address");
    }
    if (String(await web3.eth.getChainId()) !== context.chainId)
        throw new Error("KMS registry chain mismatch");
    const getter = (name, type) => ({ type: "function", name, stateMutability: "view", inputs: [], outputs: [{ type }] });
    const registry = new web3.eth.Contract([getter("kmsAppFactory", "address"), getter("expectedKeyProviderHash", "bytes32")], context.registry);
    const factoryAddress = String(await registry.methods.kmsAppFactory().call());
    if (!/^0x[a-fA-F0-9]{40}$/.test(factoryAddress) || /^0x0{40}$/.test(factoryAddress))
        throw new Error("Shared KMS policy is not configured");
    // The event payload is measured by the OS. Enrollment verifies it again via DCAP replay.
    const events = info.tcb_info.event_log.filter(event => event.imr === 3 && event.event === "key-provider");
    if (events.length !== 1 || web3.utils.keccak256(`0x${events[0].event_payload}`) !== String(await registry.methods.expectedKeyProviderHash().call()).toLowerCase()) {
        throw new Error("Workload is not provisioned by the trusted KMS");
    }
    const factory = new web3.eth.Contract([{ type: "function", name: "isApprovedApp", stateMutability: "view",
            inputs: [{ type: "address", name: "appId" }, { type: "bytes32", name: "composeHash" }], outputs: [{ type: "bool" }] }], factoryAddress);
    if (!await factory.methods.isApprovedApp(context.appId, context.composeHash).call())
        throw new Error("App is not protected by the shared KMS policy");
    return context;
};
export const initializeKmsRuntimeKeys = async (env = process.env) => {
    await requireVolatileDirectory();
    const context = await attestKmsPolicyContext(env);
    const client = new DstackClient("/var/run/dstack.sock");
    const { key: root } = await client.getKey(KMS_KEY_PATH, "participant-state-aes256gcm");
    try {
        const bundle = await loadOrCreateKmsBundle(KMS_STATE_PATH, root, context);
        const entries = [
            [ACTION_RUNTIME_KEY, Buffer.from(bundle.action, "hex")],
            ["sello-signing.seed", Buffer.from(bundle.sello, "hex")],
            ["air-signing.seed", Buffer.from(bundle.air, "hex")],
            ["participant-private.pem", Buffer.from(bundle.rsa)],
        ];
        for (const [name, bytes] of entries) {
            try {
                await fs.writeFile(path.join(RUNTIME_KEY_DIRECTORY, name), bytes, { flag: "wx", mode: 0o600 });
            }
            finally {
                bytes.fill(0);
            }
        }
    }
    finally {
        root.fill(0);
    }
};
