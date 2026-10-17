/** dstack v0.5.9 BootAuth webhook backed by the original DstackKms contract.
 * This service evaluates policy. Only dstack-kms verifies RA-TLS/DCAP evidence
 * and releases keys; an HTTP allow response is not an attestation result.
 */
import http from "node:http";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";

const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");
const fields = [
    ["appId", "address", 20], ["composeHash", "bytes32", 32],
    ["instanceId", "address", 20], ["deviceId", "bytes32", 32],
    ["mrAggregated", "bytes32", 32], ["mrSystem", "bytes32", 32],
    ["osImageHash", "bytes32", 32],
];
const getter = (name, type) => ({type: "function", name, stateMutability: "view", inputs: [], outputs: [{type}]});
const abi = [getter("gatewayAppId", "string"), getter("appImplementation", "address"),
    ...["isAppAllowed", "isKmsAllowed"].map(name => ({type: "function", name, stateMutability: "view",
        inputs: [{name: "bootInfo", type: "tuple", components: [
            ...fields.map(([name, type]) => ({name, type})),
            {name: "tcbStatus", type: "string"}, {name: "advisoryIds", type: "string[]"},
        ]}], outputs: [{type: "bool"}, {type: "string"}],
    })),
];

export function normalizeBootInfo(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("boot info object required");
    const result = {};
    for (const [name, , size] of fields) {
        const raw = value[name];
        // dstack emits full-width, unprefixed hexadecimal strings.
        if (typeof raw !== "string" || !new RegExp(name === "instanceId"
            ? "^(?:0x)?(?:[0-9a-fA-F]{2}){0,20}$"
            : `^(?:0x)?[0-9a-fA-F]{${size * 2}}$`).test(raw)) {
            throw new Error(`invalid ${name}`);
        }
        result[name] = `0x${raw.replace(/^0x/, "").padStart(size * 2, "0")}`;
    }
    if (typeof value.tcbStatus !== "string" || !value.tcbStatus || value.tcbStatus.length > 128 ||
        !Array.isArray(value.advisoryIds) || value.advisoryIds.length > 128 ||
        value.advisoryIds.some(x => typeof x !== "string" || x.length > 256)) {
        throw new Error("explicit TCB status and advisory IDs required");
    }
    result.tcbStatus = value.tcbStatus;
    result.advisoryIds = value.advisoryIds;
    return result;
}

export async function createBackend({rpcUrl, contractAddress, chainId}) {
    if (!/^0x[0-9a-fA-F]{40}$/.test(contractAddress || "") || !chainId || !rpcUrl) {
        throw new Error("ETH_RPC_URL, KMS_CONTRACT_ADDR and EXPECTED_CHAIN_ID required");
    }
    const web3 = new Web3(new Web3.providers.HttpProvider(rpcUrl, {providerOptions: {timeout: 5000}}));
    const expected = BigInt(chainId);
    const contract = new web3.eth.Contract(abi, contractAddress);
    async function ensureChain() {
        if (await web3.eth.getChainId() !== expected) throw new Error("chain ID mismatch");
        if (await web3.eth.getCode(contractAddress) === "0x") throw new Error("KMS policy contract missing");
    }
    await ensureChain();
    return {
        async health() {
            await ensureChain();
            return {status: "ok", chainId: Number(expected), kmsContractAddr: contractAddress,
                gatewayAppId: await contract.methods.gatewayAppId().call(),
                appImplementation: await contract.methods.appImplementation().call()};
        },
        async checkBoot(bootInfo, isKms) {
            await ensureChain();
            const b = normalizeBootInfo(bootInfo);
            const response = await contract.methods[isKms ? "isKmsAllowed" : "isAppAllowed"](b).call();
            return {isAllowed: response[0] === true, reason: response[1],
                gatewayAppId: await contract.methods.gatewayAppId().call()};
        },
    };
}

export function createBootAuthServer(backend) {
    return http.createServer(async (request, response) => {
        const send = (status, value) => {response.writeHead(status, {"Content-Type": "application/json"}); response.end(JSON.stringify(value));};
        const deny = (status, reason) => send(status, {isAllowed: false, reason, gatewayAppId: ""});
        if (request.method === "GET" && request.url === "/") {
            try {send(200, await backend.health());} catch {deny(503, "policy backend unavailable");}
            return;
        }
        if (request.method !== "POST" || !["/bootAuth/app", "/bootAuth/kms"].includes(request.url)) {
            deny(404, "unknown endpoint"); return;
        }
        if (!/^application\/json(?:;|$)/i.test(request.headers["content-type"] || "")) {
            deny(415, "application/json required"); return;
        }
        let size = 0;
        const chunks = [];
        try {
            for await (const chunk of request) {
                size += chunk.length;
                if (size > 32768) {deny(413, "request too large"); return;}
                chunks.push(chunk);
            }
            const info = normalizeBootInfo(JSON.parse(Buffer.concat(chunks).toString("utf8")));
            try {send(200, await backend.checkBoot(info, request.url === "/bootAuth/kms"));}
            catch {deny(200, "policy backend unavailable");}
        } catch {deny(400, "invalid boot info");}
    });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    const backend = await createBackend({rpcUrl: process.env.ETH_RPC_URL,
        contractAddress: process.env.KMS_CONTRACT_ADDR, chainId: process.env.EXPECTED_CHAIN_ID});
    const server = createBootAuthServer(backend);
    server.requestTimeout = 10000;
    server.headersTimeout = 10000;
    server.listen(Number(process.env.PORT || 8000), process.env.HOST || "127.0.0.1", () => console.log("BootAuth ready"));
    for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => server.close());
}
