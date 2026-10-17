/** Real HTTP -> BootAuth -> Anvil -> upstream DstackKms -> VITA-FL AppAuth.
 * BootInfo inputs are synthetic. This is NOT a DCAP/secret-release experiment.
 */
import test from "node:test";
import assert from "node:assert/strict";
import {spawn, spawnSync} from "node:child_process";
import {once} from "node:events";
import {readFile} from "node:fs/promises";
import {setTimeout as delay} from "node:timers/promises";
import net from "node:net";
import {createRequire} from "node:module";
import {createBackend, createBootAuthServer} from "./boot-auth.mjs";

const require = createRequire(new URL("../dfl/node_server/package.json", import.meta.url));
const {Web3} = require("web3");
const anvil = process.env.ANVIL_BINARY || "anvil";
const available = spawnSync(anvil, ["--version"], {stdio: "ignore"}).status === 0;
const required = process.env.REQUIRE_ANVIL_TESTS === "1" || process.env.CI === "true";

test("real Anvil BootAuth permits unchanged apps and denies substitution, bad TCB and untrusted KMS", {
    skip: available || required ? false : "Anvil required", timeout: 60000,
}, async t => {
    assert.ok(available, "Anvil required");
    const reservation = net.createServer().listen(0, "127.0.0.1"); await once(reservation, "listening");
    const port = reservation.address().port;
    await new Promise(resolve => reservation.close(resolve));
    const child = spawn(anvil, ["--quiet", "--host", "127.0.0.1", "--port", String(port)], {stdio: "ignore"});
    t.after(async () => {if (child.exitCode === null) {const done = once(child, "exit"); child.kill("SIGTERM"); await done;}});
    const rpcUrl = `http://127.0.0.1:${port}`;
    const web3 = new Web3(rpcUrl);
    for (let i = 0; ; i++) {
        try {await web3.eth.getChainId(); break;} catch (error) {if (i > 100) throw error; await delay(25);}
    }
    const [owner, operator, other] = await web3.eth.getAccounts();
    const artifactRoot = process.env.KMS_ARTIFACT_DIR || new URL("../smart_contracts/out/", import.meta.url).pathname;
    async function artifact(name) {return JSON.parse(await readFile(`${artifactRoot}/${name}.sol/${name}.json`, "utf8"));}
    async function deploy(name, args = []) {
        const a = await artifact(name);
        return new web3.eth.Contract(a.abi).deploy({data: a.bytecode.object, arguments: args}).send({from: owner, gas: "25000000"});
    }
    async function send(method, from = owner) {return method.send({from, gas: "20000000"});}
    const registry = await deploy("DeviceRegistry", [web3.utils.keccak256("own-kms integration")]);
    const implementation = await deploy("DstackKms");
    const proxy = await deploy("ERC1967Proxy", [implementation.options.address,
        implementation.methods.initialize(owner, `0x${"00".repeat(20)}`).encodeABI()]);
    const kms = new web3.eth.Contract((await artifact("DstackKms")).abi, proxy.options.address);
    const factory = await deploy("WorkerAppAuthFactory", [registry.options.address, kms.options.address]);
    const image = "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
    const manifest = Buffer.from(JSON.stringify({docker_compose_file: "services:\n  dfl-worker:\n    image: ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:"
        + image + '\n    environment:\n      ROUND: "1"\n', manifest_version: 2, name: "worker-0", runner: "docker-compose"}));
    const compose = `0x${manifest.toString("hex")}`;
    await send(registry.methods.setExpectedWorkerImageDigest(`0x${image}`));
    const policy = await registry.methods.workerPolicyHash(compose).call();
    await send(registry.methods.setWorkerPolicyHashAllowed(policy, true));
    await send(registry.methods.setKmsAppFactory(factory.options.address, web3.utils.keccak256("trusted-kms")));
    const os = `0x${"66".repeat(32)}`;
    await send(kms.methods.addOsImageHash(os));
    async function createApp(from) {
        const tx = await send(factory.methods.createApp(compose, `0x${"00".repeat(32)}`), from);
        return tx.events.WorkerAppCreated.returnValues.appId;
    }
    const appId = await createApp(operator);
    const backend = await createBackend({rpcUrl, contractAddress: kms.options.address, chainId: "31337"});
    await assert.rejects(createBackend({rpcUrl, contractAddress: kms.options.address, chainId: "8453"}), /chain ID mismatch/);
    await assert.rejects(createBackend({rpcUrl, contractAddress: other, chainId: "31337"}), /contract missing/);
    const server = createBootAuthServer(backend).listen(0, "127.0.0.1"); await once(server, "listening");
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const b = {appId, composeHash: "", instanceId: "", deviceId: "33".repeat(32),
        mrAggregated: "44".repeat(32), mrSystem: "55".repeat(32), osImageHash: os, tcbStatus: "UpToDate", advisoryIds: []};
    // Compose identity is SHA-256, not Ethereum keccak256.
    const {createHash} = await import("node:crypto");
    b.composeHash = createHash("sha256").update(manifest).digest("hex");
    const post = async (value, type = "app") => (await fetch(`${base}/bootAuth/${type}`, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)})).json();
    assert.equal((await post(b)).isAllowed, true);
    assert.equal((await post({...b, instanceId: "77".repeat(20)})).isAllowed, true, "unchanged workload after restart");
    assert.equal((await post({...b, composeHash: "88".repeat(32)})).isAllowed, false);
    assert.equal((await post({...b, tcbStatus: "OutOfDate"})).isAllowed, false);
    assert.equal((await post({...b, osImageHash: "99".repeat(32)})).isAllowed, false);
    assert.equal((await post({...b, appId: other})).isAllowed, false);
    assert.equal((await post({...b, appId: await createApp(other)})).isAllowed, true, "no operator preselection");
    assert.equal((await post(b, "kms")).isAllowed, false);
    await send(kms.methods.addKmsAggregatedMr(`0x${b.mrAggregated}`));
    await send(kms.methods.addKmsDevice(`0x${b.deviceId}`));
    assert.equal((await post(b, "kms")).isAllowed, true);
    assert.equal((await post({...b, mrAggregated: "aa".repeat(32)}, "kms")).isAllowed, false);
    await send(registry.methods.setWorkerPolicyHashAllowed(policy, false));
    assert.equal((await post(b)).isAllowed, false, "shared policy revocation reaches KMS gate");
});
