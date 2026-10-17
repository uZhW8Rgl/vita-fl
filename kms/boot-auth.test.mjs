import test from "node:test";
import assert from "node:assert/strict";
import {once} from "node:events";
import {createBootAuthServer, normalizeBootInfo} from "./boot-auth.mjs";

export const bootInfo = {appId: "11".repeat(20), composeHash: "22".repeat(32),
    instanceId: "", deviceId: "33".repeat(32), mrAggregated: "44".repeat(32),
    mrSystem: "55".repeat(32), osImageHash: "66".repeat(32), tcbStatus: "UpToDate", advisoryIds: []};

test("dstack boot info preserves TCB and measurements; malformed values fail", () => {
    const b = normalizeBootInfo(bootInfo);
    assert.equal(b.instanceId, `0x${"00".repeat(20)}`);
    assert.equal(b.tcbStatus, "UpToDate");
    assert.equal(normalizeBootInfo({...bootInfo, tcbStatus: "OutOfDate"}).tcbStatus, "OutOfDate");
    for (const mutation of [{appId: "ff"}, {composeHash: "zz".repeat(32)}, {mrSystem: undefined},
        {tcbStatus: undefined}, {advisoryIds: "ignored"}, {deviceId: "11".repeat(33)}]) {
        assert.throws(() => normalizeBootInfo({...bootInfo, ...mutation}));
    }
});

test("HTTP webhook routes app and KMS decisions and fails closed on chain errors", async t => {
    const calls = [];
    let failing = false;
    const server = createBootAuthServer({health: async () => {if (failing) throw Error(); return {status: "ok"};},
        checkBoot: async (b, isKms) => {if (failing) throw Error(); calls.push({b, isKms});
            return {isAllowed: !isKms, reason: isKms ? "not approved" : "", gatewayAppId: ""};}});
    server.listen(0, "127.0.0.1"); await once(server, "listening");
    t.after(() => new Promise(resolve => server.close(resolve)));
    const base = `http://127.0.0.1:${server.address().port}`;
    const post = (path, value) => fetch(base + path, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
    assert.equal((await (await post("/bootAuth/app", bootInfo)).json()).isAllowed, true);
    assert.equal((await (await post("/bootAuth/kms", bootInfo)).json()).isAllowed, false);
    assert.deepEqual(calls.map(x => x.isKms), [false, true]);
    assert.equal((await post("/bootAuth/app", {...bootInfo, tcbStatus: undefined})).status, 400);
    assert.equal((await post("/bootAuth/app", {padding: "x".repeat(40000)})).status, 413);
    assert.equal((await post("/unknown", bootInfo)).status, 404);
    failing = true;
    assert.equal((await (await post("/bootAuth/app", bootInfo)).json()).isAllowed, false);
    assert.equal((await fetch(base)).status, 503);
});
