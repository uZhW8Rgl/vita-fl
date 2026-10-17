/** Public metadata inspected through an explicitly pinned KMS root CA.
 * The CA must have been checked against the KMS's genuine bootstrap attestation.
 */
import https from "node:https";
import {X509Certificate} from "node:crypto";
import fs from "node:fs/promises";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";
const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");

export function keyProviderIdentity(caPem) {
    const certificate = new X509Certificate(caPem);
    if (!certificate.ca || certificate.publicKey.asymmetricKeyType !== "ec" ||
        certificate.publicKey.asymmetricKeyDetails.namedCurve !== "prime256v1") {
        throw new Error("Expected dstack P-256 root CA");
    }
    const id = certificate.publicKey.export({type: "spki", format: "der"}).toString("hex");
    // serde_json serializes KeyProviderInfo fields in this order in dstack 0.5.9.
    const payload = Buffer.from(JSON.stringify({name: "kms", id}));
    return {id, payload: payload.toString("hex"), hash: Web3.utils.keccak256(payload)};
}

export async function inspectKms(kmsUrl, caPem) {
    const base = new URL(kmsUrl);
    if (base.protocol !== "https:" || base.username || base.password || base.search || base.hash || base.pathname !== "/") {
        throw new Error("KMS URL must be an HTTPS origin without credentials");
    }
    const identity = keyProviderIdentity(caPem);
    const meta = await new Promise((resolve, reject) => {
        const request = https.request(new URL("/prpc/GetMeta?json", base), {method: "POST", ca: caPem,
            minVersion: "TLSv1.2", timeout: 10000, headers: {"Content-Type": "application/json"}}, response => {
            const chunks = []; let size = 0;
            response.on("data", chunk => {
                size += chunk.length;
                if (size > 262144) {response.destroy(Error("KMS metadata too large")); return;}
                chunks.push(chunk);
            });
            response.on("error", reject);
            response.on("end", () => {
                try {
                    if (response.statusCode !== 200) throw Error("KMS metadata unavailable");
                    resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
                } catch (error) {reject(error);}
            });
        });
        request.on("timeout", () => request.destroy(Error("KMS metadata timeout")));
        request.on("error", reject);
        request.end("{}");
    });
    if (meta.is_dev !== false || !Number.isSafeInteger(Number(meta.chain_id)) || Number(meta.chain_id) <= 0 ||
        !/^0x[0-9a-fA-F]{40}$/.test(meta.kms_contract_address || "") ||
        keyProviderIdentity(meta.ca_cert).hash !== identity.hash) {
        throw new Error("KMS metadata does not match the pinned real KMS");
    }
    return {chain_id: Number(meta.chain_id), kms_contract_address: meta.kms_contract_address,
        key_provider_id: identity.id, key_provider_hash: identity.hash};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    const [url, caPath] = process.argv.slice(2);
    if (!url || !caPath) throw Error("Usage: inspect-kms.mjs https://kms.example root-ca.crt");
    console.log(JSON.stringify(await inspectKms(url, await fs.readFile(caPath, "utf8")), null, 2));
}
