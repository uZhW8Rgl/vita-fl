import test from "node:test";
import assert from "node:assert/strict";
import {
  canCloseRoundInputs,
  requireClosedRoundInputs,
  shouldDeferAggregatorTimeout,
  waitForRoundInputs,
} from "../dist/state_timing.js";

const policy = (overrides = {}) => ({
  opened: true,
  closed: false,
  openedAt: 100,
  deadline: 120,
  requiredSubmissions: 5,
  acceptedSubmissions: 1,
  // Intentionally never advanced in the timeout tests.
  chainTimestamp: 100,
  ...overrides,
});

test("aggregator starts at the selected client target before the deadline", async () => {
  let slept = false;
  const result = await waitForRoundInputs({
    round: 1,
    readPolicy: async round => {
      assert.equal(round, 1);
      return policy({ requiredSubmissions: 2, acceptedSubmissions: 2 });
    },
    countModels: async () => 2,
    nowMs: () => 105_000,
    sleep: async () => { slept = true; },
  });
  assert.equal(slept, false);
  assert.equal(result.present, 2);
  assert.equal(canCloseRoundInputs(result.policy, result.present, 105_000), true);
});

test("local timer releases a partial set while the latest block never advances", async () => {
  let now = 119_500;
  const sleeps = [];
  const result = await waitForRoundInputs({
    round: 3,
    readPolicy: async () => policy(),
    countModels: async () => 1,
    nowMs: () => now,
    sleep: async ms => { sleeps.push(ms); now += ms; },
  });
  assert.deepEqual(sleeps, [500]);
  assert.equal(now, 120_000);
  assert.equal(result.policy.chainTimestamp, 100);
  assert.equal(canCloseRoundInputs(result.policy, result.present, now), true);
  assert.equal(requireClosedRoundInputs({ ...result.policy, closed: true }, 3), 1);
});

test("restarting the wait after expiry preserves the existing round deadline", async () => {
  const result = await waitForRoundInputs({
    round: 3,
    readPolicy: async () => policy({ acceptedSubmissions: 2 }),
    countModels: async () => 2,
    nowMs: () => 125_000,
    sleep: async () => assert.fail("an expired round must not get a fresh window"),
  });
  assert.equal(canCloseRoundInputs(result.policy, result.present, 125_000), true);
});

test("deadline with zero submissions ends waiting without fabricating an aggregation", async () => {
  const result = await waitForRoundInputs({
    round: 3,
    readPolicy: async () => policy({ acceptedSubmissions: 0 }),
    countModels: async () => 0,
    nowMs: () => 120_000,
    sleep: async () => assert.fail("empty expired round must leave the collection loop"),
  });
  assert.equal(canCloseRoundInputs(result.policy, 0, 120_000), false);
  assert.throws(() => requireClosedRoundInputs({ ...result.policy, closed: true }, 3), /nonempty/);
});

test("local deadline cannot bypass missing authenticated model files", () => {
  assert.equal(canCloseRoundInputs(policy({ acceptedSubmissions: 2 }), 1, 120_000), false);
  assert.equal(canCloseRoundInputs(policy(), 1, 119_999), false);
  assert.equal(canCloseRoundInputs(policy({ opened: false }), 1, 125_000), false);
});

test("publication uses the actual closed count and preserves the bootstrap exception", () => {
  assert.equal(requireClosedRoundInputs(policy({ closed: true, acceptedSubmissions: 2 }), 3), 2);
  assert.equal(requireClosedRoundInputs(policy({ closed: true, acceptedSubmissions: 0 }), 0), 0);
  assert.throws(() => requireClosedRoundInputs(policy(), 3), /not immutably closed/);
  assert.throws(() => requireClosedRoundInputs(policy({ closed: true, acceptedSubmissions: NaN }), 3), /nonempty/);
});

test("expired collection gets an aggregation allowance before timeout reporting", () => {
  assert.equal(shouldDeferAggregatorTimeout(policy(), 30_000, 110_000), true);
  assert.equal(shouldDeferAggregatorTimeout(policy(), 30_000, 120_000), true);
  assert.equal(shouldDeferAggregatorTimeout(policy({ closed: true }), 30_000, 125_000), true);
  assert.equal(shouldDeferAggregatorTimeout(policy(), 30_000, 150_000), false);
  // Early closure need not wait until a distant UI-selected deadline to detect a stall.
  assert.equal(shouldDeferAggregatorTimeout(policy({ closed: true, deadline: 3600 }), 30_000, 125_000), false);
});
