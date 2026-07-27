import test from "node:test";
import assert from "node:assert/strict";

import {
    dstackHttpsEndpoint,
    normalizeDstackAppId,
} from "../dist/runtime_endpoints.js";

const APP_ID = "181195df89a143ecaf3d38b2cf0fcb8a30310f07";

test("dstack inference endpoint is derived from the live app id", () => {
    assert.equal(normalizeDstackAppId(`app_${APP_ID.toUpperCase()}`), APP_ID);
    assert.equal(
        dstackHttpsEndpoint({
            appId: `app_${APP_ID}`,
            port: 8080,
            gatewayDomain: ".dstack-pha-prod9.phala.network",
        }),
        `https://${APP_ID}-8080.dstack-pha-prod9.phala.network`,
    );
});

test("dstack endpoint derivation rejects malformed identity inputs", () => {
    assert.throws(
        () => dstackHttpsEndpoint({
            appId: "app_not-an-id",
            port: 8080,
            gatewayDomain: "dstack-pha-prod9.phala.network",
        }),
        /Invalid dstack app_id/,
    );
    assert.throws(
        () => dstackHttpsEndpoint({
            appId: APP_ID,
            port: 0,
            gatewayDomain: "dstack-pha-prod9.phala.network",
        }),
        /Invalid dstack gateway port/,
    );
    assert.throws(
        () => dstackHttpsEndpoint({
            appId: APP_ID,
            port: 8080,
            gatewayDomain: "https://dstack-pha-prod9.phala.network",
        }),
        /Invalid dstack gateway domain/,
    );
});
