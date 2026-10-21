/** Private signing seeds shared only by processes of one participant workload. */
import crypto from "crypto";
import fs from "fs/promises";
import path from "path";
import { pathToFileURL } from "url";

export const RUNTIME_KEY_DIRECTORY = "/run/vita-fl";
export const SELLO_RUNTIME_KEY = "sello-signing.seed";
export const AIR_RUNTIME_KEY = "air-signing.seed";
const TMPFS_MAGIC = 0x01021994;

export const requireVolatileDirectory = async (directory = RUNTIME_KEY_DIRECTORY) => {
    const actual = await fs.realpath(directory);
    if (actual !== directory) throw new Error("Runtime key directory must not be a symbolic link.");
    const stat = await fs.stat(directory);
    if (!stat.isDirectory() || (stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()) {
        throw new Error("Runtime key directory must be owned by this process user and private.");
    }
    if ((await fs.statfs(directory)).type !== TMPFS_MAGIC) {
        throw new Error("Runtime private keys require a tmpfs mount.");
    }
    // tmpfs can otherwise be swapped to disk. This is a deployment precondition.
    const swaps = await fs.readFile("/proc/swaps", "utf8");
    if (swaps.trim().split("\n").length > 1) throw new Error("Runtime private keys require swap to be disabled.");
};

export const initializeRuntimeSigningKeys = async (directory = RUNTIME_KEY_DIRECTORY) => {
    await requireVolatileDirectory(directory);
    const created: string[] = [];
    try {
        for (const name of [SELLO_RUNTIME_KEY, AIR_RUNTIME_KEY]) {
            const filename = path.join(directory, name);
            const seed = crypto.randomBytes(32);
            let handle;
            try {
                handle = await fs.open(filename, "wx", 0o600);
                created.push(filename);
                await handle.writeFile(seed);
            } finally {
                seed.fill(0);
                await handle?.close();
            }
        }
    } catch (error) {
        for (const filename of created) await fs.rm(filename, { force: true });
        throw error;
    }
};

export const readRuntimeSigningSeed = async (name: string, directory = RUNTIME_KEY_DIRECTORY) => {
    if (![SELLO_RUNTIME_KEY, AIR_RUNTIME_KEY, "action-root.seed"].includes(name)) throw new Error("Unknown runtime signing key.");
    await requireVolatileDirectory(directory);
    const filename = path.join(directory, name);
    const stat = await fs.lstat(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) {
        throw new Error("Runtime signing seed must be a private regular file.");
    }
    const seed = await fs.readFile(filename);
    if (seed.length !== 32) { seed.fill(0); throw new Error("Runtime signing seed must contain 32 bytes."); }
    return seed;
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    if (process.env.PARTICIPANT_KEY_PROVIDER === "broker") {
        const { initializeAttestedRuntimeKeys } = await import("./attested_keys.js");
        await initializeAttestedRuntimeKeys();
    } else if (process.env.PARTICIPANT_KEY_PROVIDER === "kms") {
        const { initializeKmsRuntimeKeys } = await import("./kms_keys.js");
        await initializeKmsRuntimeKeys();
    } else {
        await initializeRuntimeSigningKeys();
    }
}
