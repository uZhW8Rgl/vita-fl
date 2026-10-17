/** Create a worker policy for an OWN KMS, including Anvil. No hosted-KMS fallback.
 * The returned app_id is passed to dstack VMM CreateVm/--app-id, not to the
 * managed PHALA-KMS commit API, which has its own app-ID restrictions.
 */
import fs from "node:fs/promises";
import crypto from "node:crypto";
import {createRequire} from "node:module";
import {inspectKms} from "./inspect-kms.mjs";
import {registerApp} from "../phala/register-kms-app.mjs";

const [manifestPath, kmsUrl, caPath] = process.argv.slice(2);
if (!manifestPath || !kmsUrl || !caPath) throw Error("Usage: register-worker.mjs app-compose.json https://kms.example root-ca.crt");
const manifest = await fs.readFile(manifestPath);
const meta = await inspectKms(kmsUrl, await fs.readFile(caPath, "utf8"));
const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");
const web3 = new Web3(process.env.RPC_URL);
const registry = new web3.eth.Contract([{type: "function", name: "expectedKeyProviderHash", stateMutability: "view",
    inputs: [], outputs: [{type: "bytes32"}]}], process.env.REGISTRY_ADDRESS);
if (String(await registry.methods.expectedKeyProviderHash().call()).toLowerCase() !== meta.key_provider_hash.toLowerCase()) {
    throw Error("Registry does not trust this KMS root; configure it before creating workers");
}
const payload = await registerApp(manifest, {compose_hash: crypto.createHash("sha256").update(manifest).digest("hex"),
    kms_info: meta}, process.env, {ownKms: true});
console.log(JSON.stringify({...payload, kms_urls: [kmsUrl], key_provider_hash: meta.key_provider_hash}, null, 2));
