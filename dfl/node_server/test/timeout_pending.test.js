import test from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import Web3 from "web3";

const anvil = process.env.ANVIL_BINARY || "anvil";
const available = spawnSync(anvil, ["--version"], { stdio: "ignore" }).status === 0;
const required = process.env.REQUIRE_ANVIL_TESTS === "1" || process.env.CI === "true";
const probe = JSON.parse(await readFile(new URL("fixtures/timeout_gas_probe.json", import.meta.url), "utf8"));

async function unusedPort() {
    const server = net.createServer();
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const port = server.address().port;
    await new Promise((resolve) => server.close(resolve));
    return port;
}

test("timeout report estimates the pending block when Anvil latest predates its deadline", {
    skip: available || required ? false : "Anvil is required; put it on PATH or set ANVIL_BINARY",
    timeout: 20000,
}, async (t) => {
    assert.ok(available, "Anvil is required; put it on PATH or set ANVIL_BINARY");
    const port = await unusedPort();
    const child = spawn(anvil, ["--quiet", "--host", "127.0.0.1", "--port", String(port)], {
        stdio: "ignore",
    });
    t.after(async () => {
        if (child.exitCode === null) {
            const exited = once(child, "exit");
            child.kill("SIGTERM");
            await exited;
        }
    });
    const rpcUrl = `http://127.0.0.1:${port}`;
    let sequence = 0;
    async function rpc(method, params = []) {
        const response = await fetch(rpcUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", id: ++sequence, method, params }),
        });
        const value = await response.json();
        if (value.error) throw new Error(value.error.message);
        return value.result;
    }
    for (let attempt = 0; ; attempt++) {
        try {
            await rpc("eth_chainId");
            break;
        } catch (error) {
            if (attempt >= 100 || child.exitCode !== null) throw error;
            await delay(20);
        }
    }

    const web3 = new Web3(rpcUrl);
    const [deployer] = await web3.eth.getAccounts();
    const reporter = web3.eth.accounts.privateKeyToAccount(`0x${"11".repeat(32)}`);
    await rpc("anvil_setBalance", [reporter.address, "0x56bc75e2d63100000"]);
    const deadline = Math.floor(Date.now() / 1000) + 2;
    const contract = await new web3.eth.Contract(probe.abi).deploy({
        data: probe.bytecode,
        arguments: [deadline, deployer],
    }).send({ from: deployer, gas: "1000000" });
    const before = await web3.eth.getBlock("latest");
    assert.ok(before.timestamp <= BigInt(deadline));
    while (Date.now() <= (deadline + 1) * 1000) await delay(20);
    const stillLatest = await web3.eth.getBlock("latest");
    assert.equal(stillLatest.number, before.number, "No idle block must be mined to advance this test");
    assert.equal(stillLatest.timestamp, before.timestamp);

    const method = contract.methods.reportAggregatorTimeout(1, deployer);
    await assert.rejects(method.estimateGas({ from: reporter.address }), (error) => {
        assert.match(String(error.cause?.message || error.message), /submission window is still active/);
        return true;
    });
    const pendingGas = await web3.eth.estimateGas({
        from: reporter.address,
        to: contract.options.address,
        data: method.encodeABI(),
    }, "pending");
    assert.ok(pendingGas > 21000n);

    const output = await mkdtemp(path.join(os.tmpdir(), "vita-fl-timeout-pending-"));
    t.after(() => rm(output, { recursive: true, force: true }));
    const environment = {
        RPC_URL: rpcUrl,
        AGGREGATOR_ADDRESS: contract.options.address,
        GM_STORAGE_ADDRESS: contract.options.address,
        EXPECTED_CHAIN_ID: "31337",
        DFL_TRANSACTION_COST_CSV: path.join(output, "transaction-costs.csv"),
        OTEL_ENABLED: "false",
    };
    const previous = Object.fromEntries(Object.keys(environment).map((name) => [name, process.env[name]]));
    Object.assign(process.env, environment);
    t.after(() => {
        for (const [name, value] of Object.entries(previous)) {
            if (value === undefined) delete process.env[name];
            else process.env[name] = value;
        }
    });
    const client = await import(`../dist/bc_client.js?timeout-pending-${port}`);
    client.configureParticipantActionSigner({
        address: reporter.address,
        signTransaction: async (transaction) => {
            const signed = await reporter.signTransaction(transaction);
            return signed.rawTransaction;
        },
    });
    const receipt = await client.reportAggregatorTimeout(1, deployer);
    assert.equal(receipt.status, 1n);
    assert.equal(await contract.methods.reports().call(), 1n);
    const mined = await web3.eth.getBlock(receipt.blockNumber);
    assert.ok(mined.timestamp > BigInt(deadline));
});
