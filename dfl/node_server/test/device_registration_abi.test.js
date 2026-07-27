import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const registryAbi = JSON.parse(
    fs.readFileSync(new URL("../abi/registry.json", import.meta.url), "utf8"),
);

test("current-registration ABI binds both registered endpoints", () => {
    const entries = registryAbi.filter(
        (entry) => entry.type === "function" && entry.name === "isDeviceRegistrationCurrent",
    );
    assert.equal(entries.length, 1);
    assert.deepEqual(
        entries[0].inputs.map(({ name, type }) => ({ name, type })),
        [
            { name: "_address", type: "address" },
            { name: "_public_ip", type: "string" },
            { name: "_msg_broker_ip", type: "string" },
            { name: "_public_key", type: "bytes" },
            { name: "canonicalAppCompose", type: "bytes" },
        ],
    );
});
