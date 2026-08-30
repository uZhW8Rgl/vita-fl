import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/server.ts", import.meta.url),
  "utf8",
);

const helperStart = source.indexOf(
  "async function prepareAggregatorParentModel(currentRound, expectedState)",
);
const helperEnd = source.indexOf(
  "\nfunction isMissingRoundKeyError",
  helperStart,
);
const helper = source.slice(helperStart, helperEnd);

test("aggregator authenticates the active parent model fail-closed", () => {
  assert.notEqual(helperStart, -1);
  assert.notEqual(helperEnd, -1);
  assert.match(helper, /getCurrentModel\(activeParticipantKey\.privateKey\)/);
  assert.match(
    helper,
    /assertFetchedGlobalModelIsStillCurrent\(\s*fetchedGlobalModel,\s*\)/,
  );
  assert.match(
    helper,
    /Number\(currentGlobalModel\.modelRound\) !== currentRound/,
  );
  assert.match(helper, /Number\(currentGlobalModel\.modelRound\) !== 1/);
  assert.match(helper, /verifyDownloadedGlobalModelSignature\(\{/);
  assert.match(helper, /latestState\["0"\] !== expectedState/);
  assert.match(helper, /Number\(latestRound\) !== currentRound/);
  assert.match(
    helper,
    /!sameAddress\(latestState\["1"\], process\.env\.ACCOUNT_ADDRESS\)/,
  );
});

test("aggregator prepares the parent before opening round submissions", () => {
  const aggregatorBranch = source.indexOf(
    'console.log("I am the aggregator");',
    source.indexOf('case "TRAINING"'),
  );
  const prepare = source.indexOf(
    'await prepareAggregatorParentModel(currentRound, "TRAINING")',
    aggregatorBranch,
  );
  const open = source.indexOf(
    "await openModelSubmissions(currentRound)",
    aggregatorBranch,
  );

  assert.notEqual(aggregatorBranch, -1);
  assert.notEqual(prepare, -1);
  assert.notEqual(open, -1);
  assert.ok(prepare < open);

  const gate = source.slice(
    source.lastIndexOf("if (currentRound > 0)", prepare),
    open,
  );
  assert.match(
    gate,
    /await prepareAggregatorParentModel\(currentRound, "TRAINING"\)/,
  );
  assert.match(gate, /keeping submissions closed/);
  assert.match(gate, /continue;/);
});

test("aggregator restores and authenticates the parent after AGGREGATING restart", () => {
  const aggregatingBranch = source.indexOf('case "AGGREGATING"');
  const prepare = source.indexOf(
    "await prepareAggregatorParentModel(",
    aggregatingBranch,
  );
  const policy = source.indexOf(
    "await getRoundAggregationPolicy(currentRound)",
    aggregatingBranch,
  );
  const aggregate = source.indexOf("callPythonService('/aggregate'", aggregatingBranch);

  assert.notEqual(aggregatingBranch, -1);
  assert.notEqual(prepare, -1);
  assert.notEqual(policy, -1);
  assert.notEqual(aggregate, -1);
  assert.ok(prepare < policy);
  assert.ok(prepare < aggregate);

  const preparation = source.slice(prepare, policy);
  assert.match(preparation, /currentRound,\s*"AGGREGATING",/);
});
