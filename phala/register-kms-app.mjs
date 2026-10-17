/** Phase between Phala provision and commit; uses each operator's own wallet.
 * Never signs with the DFL registry-owner key or approves participant addresses.
 * Usage: KMS_DEPLOYER_PRIVATE_KEY=... RPC_URL=... REGISTRY_ADDRESS=...
 *   node phala/register-kms-app.mjs exact-app-compose.json provision.json
 * Prints a public commit payload; encrypted envs must be added through Phala SDK.
 */
import fs from "node:fs/promises";
import crypto from "node:crypto";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";

const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");
const getter = (name, type) => ({type: "function", name, stateMutability: "view", inputs: [], outputs: [{type}]});
const hex = value => `0x${String(value).replace(/^0x/, "")}`.toLowerCase();

export function validateProvision(manifest, provision, chainId, {ownKms = false} = {}) {
    const digest = `0x${crypto.createHash("sha256").update(manifest).digest("hex")}`;
    if (digest !== hex(provision.compose_hash)) throw new Error("Exact app_compose bytes do not match Phala provision; do not reserialize JSON");
    if ((!ownKms && ![1, 8453].includes(Number(chainId))) || Number(provision.kms_info?.chain_id) !== Number(chainId)) {
        throw new Error("Registry must be on the selected supported onchain KMS chain; PHALA/Anvil is not a substitute");
    }
    return digest;
}

export async function registerApp(manifest, provision, env = process.env, options = {}) {
    const web3 = new Web3(env.RPC_URL);
    const chainId = await web3.eth.getChainId();
    const composeHash = validateProvision(manifest, provision, chainId, options);
    if (!/^0x[0-9a-fA-F]{64}$/.test(env.KMS_DEPLOYER_PRIVATE_KEY || "")) throw new Error("Operator wallet key required");
    const registry = new web3.eth.Contract([getter("kmsAppFactory", "address")], env.REGISTRY_ADDRESS);
    const factoryAddress = await registry.methods.kmsAppFactory().call();
    if (/^0x0{40}$/.test(factoryAddress)) throw new Error("Shared KMS policy has not been configured");
    const factory = new web3.eth.Contract([
        getter("kms", "address"), getter("registry", "address"),
        {type: "function", name: "createApp", stateMutability: "nonpayable",
            inputs: [{type: "bytes", name: "canonicalCompose"}, {type: "bytes32", name: "salt"}], outputs: [{type: "address"}]},
        {type: "event", name: "WorkerAppCreated", inputs: [
            {type: "address", name: "operator", indexed: true}, {type: "address", name: "appId", indexed: true},
            {type: "bytes32", name: "composeHash", indexed: false}]},
    ], factoryAddress);
    if (hex(await factory.methods.kms().call()) !== hex(provision.kms_info.kms_contract_address) ||
        hex(await factory.methods.registry().call()) !== hex(env.REGISTRY_ADDRESS)) throw new Error("Factory/KMS/registry binding mismatch");
    const account = web3.eth.accounts.privateKeyToAccount(env.KMS_DEPLOYER_PRIVATE_KEY);
    web3.eth.accounts.wallet.add(account);
    const create = factory.methods.createApp(`0x${manifest.toString("hex")}`, `0x${crypto.randomBytes(32).toString("hex")}`);
    const gas = await create.estimateGas({from: account.address});
    const receipt = await create.send({from: account.address, gas: (gas * 130n / 100n).toString()});
    const appId = receipt.events?.WorkerAppCreated?.returnValues.appId;
    if (!appId) throw new Error("Confirmed app creation has no WorkerAppCreated event; inspect receipt before retrying");
    return {app_id: appId, compose_hash: composeHash, contract_address: appId, deployer_address: account.address,
        ...(provision.kms_id ? {kms_id: provision.kms_id} : {})};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    const [manifestPath, provisionPath] = process.argv.slice(2);
    if (!manifestPath || !provisionPath) throw new Error("Usage: register-kms-app.mjs exact-app-compose.json provision.json");
    const payload = await registerApp(await fs.readFile(manifestPath), JSON.parse(await fs.readFile(provisionPath, "utf8")));
    console.log(JSON.stringify(payload, null, 2));
}
