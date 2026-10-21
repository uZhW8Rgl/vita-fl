/** Independent attested key release; Anvil is policy storage, never key storage. */
import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import {constants} from "node:fs";
import {execFile} from "node:child_process";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";
import {canonicalClaims, deriveApplicationKey, encryptApplicationKey} from "../dfl/node_server/dist/attested_key_protocol.js";

const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");
const hexAddress = value => typeof value === "string" && /^0x[a-f0-9]{40}$/.test(value) && !/^0x0{40}$/.test(value);
const digest = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const getter = (name, type) => ({type:"function", name, stateMutability:"view", inputs:[], outputs:[{type}]});
const abi = [getter("registry", "address"), {type:"function", name:"isAllowed", stateMutability:"view",
    inputs:[{name:"participant",type:"address"},{name:"appId",type:"address"},{name:"composeHash",type:"bytes32"}], outputs:[{type:"bool"}]}];

export async function createPolicy({rpcUrl, policy, registry, codeHash}) {
    policy = String(policy || "").toLowerCase(); registry = String(registry || "").toLowerCase();
    codeHash = String(codeHash || "").toLowerCase();
    if (!hexAddress(policy) || !hexAddress(registry) || !/^0x[a-f0-9]{64}$/.test(codeHash || "")) throw new Error("Explicit policy, registry and deployed code hash required");
    const rpc = new URL(rpcUrl);
    if (!["http:","https:"].includes(rpc.protocol) || rpc.username || rpc.password) throw new Error("Invalid Anvil RPC URL");
    const web3 = new Web3(new Web3.providers.HttpProvider(rpcUrl, {providerOptions:{timeout:5000}}));
    const contract = new web3.eth.Contract(abi, policy);
    const snapshot = async () => {
        if (await web3.eth.getChainId() !== 31337n) throw new Error("Anvil chain 31337 required");
        const block = await web3.eth.getBlockNumber();
        const code = await web3.eth.getCode(policy, block);
        if (code === "0x" || web3.utils.keccak256(code) !== codeHash) throw new Error("Key policy code hash mismatch");
        if (String(await contract.methods.registry().call({}, block)).toLowerCase() !== registry) throw new Error("Key policy registry mismatch");
        return block;
    };
    await snapshot();
    return {policy, registry, async health() {await snapshot();}, async allowed(claims) {
        const block = await snapshot();
        return await contract.methods.isAllowed(claims.participant, claims.appId, "0x"+claims.composeHash).call({}, block) === true;
    }};
}

export async function readRoot(filename) {
    const file = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
        const stat = await file.stat();
        if (!stat.isFile() || stat.size !== 32 || stat.uid !== process.getuid?.() || (stat.mode & 0o077)) throw new Error("Service root must be an owned, private 32-byte regular file");
        return await file.readFile();
    } finally {await file.close();}
}

export function appraiseEvidence(evidence, claims) {
    return new Promise((resolve, reject) => {
        const child = execFile(process.env.KEY_SERVICE_PYTHON || "python3", ["-m","key_service.verify"],
            {timeout:35000,maxBuffer:65536}, (error, stdout) => {
                if (error) {reject(new Error("Attestation rejected")); return;}
                try {
                    const result = JSON.parse(stdout);
                    if (result.verified !== true || result.appId !== claims.appId || result.composeHash !== claims.composeHash) throw new Error();
                    resolve(result);
                } catch {reject(new Error("Attestation rejected"));}
            });
        child.stdin.on("error", () => {});
        child.stdin.end(JSON.stringify({claims,evidence}));
    });
}

export function createKeyServer({root, backend, serviceId, appraise = appraiseEvidence, now = Date.now, maxPending = 1024, maxVerifications = 4}) {
    if (!(root instanceof Uint8Array) || root.length !== 32 || !digest(serviceId)) throw new Error("Service root and service ID required");
    const pending = new Map();
    let verifying = 0;
    const server = http.createServer(async (request, response) => {
        const send = (status, value) => {response.writeHead(status, {"Content-Type":"application/json","Cache-Control":"no-store"});response.end(JSON.stringify(value));};
        if (request.method === "GET" && request.url === "/health") {
            try {await backend.health();send(200,{status:"ok",chainId:"31337"});} catch {send(503,{error:"policy unavailable"});} return;
        }
        if (request.method !== "POST" || !["/v1/challenge","/v1/release"].includes(request.url)) {send(404,{error:"unknown operation"});return;}
        if (!/^application\/json(?:;|$)/i.test(request.headers["content-type"] || "")) {send(415,{error:"JSON required"});return;}
        try {
            let size = 0;const chunks = [];
            for await (const chunk of request) {size += chunk.length;if(size > 524288) throw new Error();chunks.push(chunk);}
            const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            if (request.url === "/v1/challenge") {
                if (!value || Object.keys(value).sort().join() !== ["participant","appId","composeHash","registry","recipientPublicKey"].sort().join()) throw new Error();
                const claims = {...value, version:1, challenge:crypto.randomBytes(32).toString("hex"),
                    chainId:"31337", policy:backend.policy, serviceId};
                canonicalClaims(claims);
                if (claims.registry !== backend.registry) throw new Error();
                await backend.health();
                for (const [key,item] of pending) if(item.expires <= now()) pending.delete(key);
                if (pending.size >= maxPending) {send(503,{error:"challenge capacity reached"});return;}
                pending.set(claims.challenge, {claims, expires:now()+60000});
                send(200,{claims});return;
            }
            if (!value || Object.keys(value).sort().join() !== ["challenge","signature","evidence"].sort().join() || !digest(value.challenge)) throw new Error();
            const item = pending.get(value.challenge);
            if (!item || item.expires <= now()) {pending.delete(value.challenge);send(403,{error:"challenge expired or consumed"});return;}
            // Consume synchronously before any async work: concurrent reuse cannot release twice.
            pending.delete(value.challenge);
            const claims = item.claims;
            if (typeof value.signature !== "string" || !/^0x[a-fA-F0-9]{130}$/.test(value.signature)) throw new Error();
            const web3 = new Web3();
            if (web3.eth.accounts.recover(canonicalClaims(claims).toString(), value.signature).toLowerCase() !== claims.participant) {
                send(403,{error:"participant delegation rejected"});return;
            }
            if (verifying >= maxVerifications) {send(503,{error:"appraisal capacity reached"});return;}
            verifying++;
            try {
                if (!await backend.allowed(claims)) {send(403,{error:"workload policy rejected"});return;}
                const result = await appraise(value.evidence, claims);
                if (!result || result.verified !== true || result.appId !== claims.appId || result.composeHash !== claims.composeHash) throw new Error();
                // Recheck revocation after remote quote verification; derive only after both checks.
                if (item.expires <= now() || !await backend.allowed(claims)) {send(403,{error:"release no longer authorised"});return;}
                const key = deriveApplicationKey(root, claims);
                try {send(200,encryptApplicationKey(key,claims));} finally {key.fill(0);}
            } finally {verifying--;}
        } catch {send(403,{error:"key release rejected"});}
    });
    server.requestTimeout = 10000;server.headersTimeout = 10000;
    return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    if (process.argv[2] === "init-root") {
        if (!process.argv[3]) throw new Error("Explicit root path required");
        const root = crypto.randomBytes(32);
        try {await fs.writeFile(process.argv[3], root, {mode:0o600,flag:"wx"});} finally {root.fill(0);}
        console.log("Service root initialised; protect it independently of replaceable workloads.");
    } else {
        await new Promise((resolve,reject)=>execFile(process.env.KEY_SERVICE_PYTHON || "python3",
            ["-m","key_service.verify","--check-config"],{timeout:10000},error=>error?reject(new Error("Invalid attestation configuration")):resolve()));
        const root = await readRoot(process.env.KEY_SERVICE_ROOT_FILE);
        const backend = await createPolicy({rpcUrl:process.env.KEY_SERVICE_RPC_URL,policy:process.env.KEY_SERVICE_POLICY,
            registry:process.env.KEY_SERVICE_REGISTRY,codeHash:process.env.KEY_SERVICE_POLICY_CODE_HASH});
        const server = createKeyServer({root,backend,serviceId:process.env.KEY_SERVICE_ID});
        server.listen(Number(process.env.KEY_SERVICE_PORT || 8000),process.env.KEY_SERVICE_HOST || "127.0.0.1",()=>console.log("Attested key service ready (Anvil policy)"));
        for (const signal of ["SIGTERM","SIGINT"]) process.on(signal,()=>server.close(()=>{root.fill(0);}));
    }
}
