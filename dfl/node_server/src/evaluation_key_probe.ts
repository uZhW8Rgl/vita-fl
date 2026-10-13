/** Opt-in test workload: attest public observations without exporting any secret.
 * Run this entry point as the declared command of a dedicated test application.
 * A substituted image needs its own real quote; approval is checked externally.
 */
import crypto from "crypto";
import http from "http";
import { pathToFileURL } from "url";
import { DstackClient } from "@phala/dstack-sdk";
import { loadParticipantKey, type ParticipantKey } from "./participant_key.js";

let instanceKey: Promise<ParticipantKey> | undefined;

export const probeKey = async (
    challenge: string,
    options: {
        env?: NodeJS.ProcessEnv;
        initializeKey?: () => Promise<ParticipantKey>;
        info?: () => Promise<{ app_id: string; tcb_info: { app_compose: string } }>;
        quote?: (reportData: Buffer) => Promise<{ quote: string; event_log: string }>;
    } = {},
) => {
    const env = options.env || process.env;
    if (env.EVALUATION_GATES_ENABLED !== "true" || env.DOCKER !== "phala") {
        throw new Error("Key probe requires explicit Phala evaluation configuration.");
    }
    if (!/^[a-f0-9]{64}$/.test(challenge)) throw new Error("A 32-byte challenge is required.");
    let client: DstackClient | undefined;
    const getClient = () => client ||= new DstackClient("/var/run/dstack.sock");
    const metadata = await (options.info || (() => getClient().info()))();
    // One RSA identity per probe process, not per HTTP request. A new process
    // has no persisted state from which to reconstruct this identity.
    const key = await (options.initializeKey
        ? options.initializeKey()
        : instanceKey ||= loadParticipantKey({env}));
    const claims = {
        schema_version: 3,
        challenge,
        app_id: metadata.app_id,
        app_compose_sha256: crypto.createHash("sha256").update(metadata.tcb_info.app_compose).digest("hex"),
        state_present_before: false,
        state_sha256: null,
        kms_material_obtained: false,
        key_outcome: "KEY_HELD_IN_MEMORY",
        key_lifecycle: "ephemeral-workload-v1",
        rsa_public_key_sha256: key ? crypto.createHash("sha256").update(key.publicKeyDer).digest("hex") : null,
    };
    // Fixed key order, ASCII fields. The driver checks the actual quoted bytes.
    const claimsBytes = Buffer.from(JSON.stringify(claims));
    const reportData = crypto.createHash("sha512").update("VITA-FL:key-evaluation:v1\0").update(claimsBytes).digest();
    const quote = await (options.quote || (data => getClient().getQuote(data)))(reportData);
    return { claims, claims_json: claimsBytes.toString("utf8"), quote: quote.quote,
             event_log: quote.event_log, app_compose: metadata.tcb_info.app_compose };
};

export const serveKeyProbe = () => {
    if (process.env.EVALUATION_GATES_ENABLED !== "true" || process.env.DOCKER !== "phala") {
        throw new Error("Key probe disabled.");
    }
    // Serialise requests because generating a first RSA state is not a concurrent operation.
    let busy = false;
    const server = http.createServer(async (request, response) => {
        if (request.method !== "POST" || request.url !== "/v1/evaluation/key") {
            response.writeHead(404).end(); return;
        }
        if (busy) { response.writeHead(409).end(); return; }
        busy = true;
        try {
            let bytes = 0;
            const chunks: Buffer[] = [];
            for await (const chunk of request) {
                bytes += chunk.length;
                if (bytes > 64) throw new Error("Challenge too large.");
                chunks.push(Buffer.from(chunk));
            }
            const value = await probeKey(Buffer.concat(chunks).toString("ascii"));
            response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(value));
        } catch {
            response.writeHead(503, { "Content-Type": "application/json" }).end('{"error":"KEY_PROBE_UNAVAILABLE"}');
        } finally { busy = false; }
    });
    server.requestTimeout = 30000;
    server.listen(Number(process.env.EVALUATION_KEY_PROBE_PORT || "8090"), "0.0.0.0");
    return server;
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) serveKeyProbe();
