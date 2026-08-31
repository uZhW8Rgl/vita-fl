// @ts-nocheck
import Web3 from "web3";
import fs from "fs";
import 'dotenv/config';
import { emitTelemetryEvent } from "./telemetry.js";
import { normalizePublisherPublicKeyDerHex } from "./model_publisher.js";
import { signRawDigest, } from "./action_key.js";
import { deriveAggregationStatementDigest, deriveModelSubmissionDigest, derivePublicationHash, deriveRoundAggregationPolicyHash, } from "./protocol_digest.js";
import { gweiRateToWei, parseDecimalRate, transactionCostAmounts, } from "./transaction_cost.js";
// setup client´
//const web3 = new Web3("https://eth-sepolia.g.alchemy.com/v2/pFowzUSGYob62Q7i2YVsF0LFUX3WiCT2");
const web3 = new Web3(process.env.RPC_URL);
// define smart contract addresses
const gm_storage_address = process.env.GM_STORAGE_ADDRESS;
const aggregator_address = process.env.AGGREGATOR_ADDRESS;
const device_registry_address = process.env.REGISTRY_ADDRESS;
const medical_signer_registry_address = process.env.MEDICAL_SIGNER_REGISTRY_ADDRESS;
const bootstrapPrivateKey = process.env.PRIVATE_KEY;
let participantActionSigner;
let protocolTransactionQueue = Promise.resolve();
export const configureParticipantActionSigner = (signer) => {
    if (!signer || !/^0x[0-9a-fA-F]{40}$/.test(String(signer.address || ""))) {
        throw new Error("A valid participant action signer is required.");
    }
    participantActionSigner = signer;
};
export const getParticipantActionAddress = () => {
    if (!participantActionSigner) {
        throw new Error("Participant action signer has not been configured.");
    }
    return participantActionSigner.address;
};
const jsonReplacer = (_key, value) => {
    if (typeof value === "bigint") {
        return value.toString();
    }
    return value;
};
const serializeError = (error) => ({
    name: error?.name,
    message: error?.message,
    code: error?.code,
    reason: error?.reason,
    data: error?.data,
    cause: error?.cause?.message || error?.cause,
    receipt: error?.receipt
        ? {
            status: error.receipt.status?.toString?.() ?? error.receipt.status,
            transactionHash: error.receipt.transactionHash,
            blockNumber: error.receipt.blockNumber?.toString?.() ?? error.receipt.blockNumber,
            gasUsed: error.receipt.gasUsed?.toString?.() ?? error.receipt.gasUsed,
            from: error.receipt.from,
            to: error.receipt.to,
        }
        : undefined,
});
const sameAddress = (left, right) => String(left || "").toLowerCase() === String(right || "").toLowerCase();
const logJson = (label, payload) => {
    console.log(label, JSON.stringify(payload, jsonReplacer));
};
const ethEurPrice = parseDecimalRate(process.env.ETH_EUR_PRICE || "3000", "ETH_EUR_PRICE");
const configuredEthUsdPrice = String(process.env.ETH_USD_PRICE || "").trim();
const ethUsdPrice = configuredEthUsdPrice
    ? parseDecimalRate(configuredEthUsdPrice, "ETH_USD_PRICE")
    : undefined;
const exchangeRateSource = String(process.env.EXCHANGE_RATE_SOURCE || "manual_configuration").trim();
const exchangeRateTimestampUtc = String(process.env.EXCHANGE_RATE_TIMESTAMP_UTC || "").trim();
const configuredReferenceGasPriceGwei = String(process.env.REFERENCE_MAINNET_GAS_PRICE_GWEI || "").trim();
const referenceGasPriceGwei = configuredReferenceGasPriceGwei
    ? parseDecimalRate(configuredReferenceGasPriceGwei, "REFERENCE_MAINNET_GAS_PRICE_GWEI")
    : undefined;
const referenceGasPriceSource = String(process.env.REFERENCE_GAS_PRICE_SOURCE || "").trim();
const referenceGasPriceTimestampUtc = String(process.env.REFERENCE_GAS_PRICE_TIMESTAMP_UTC || "").trim();
const transactionCostCsvPath = process.env.DFL_TRANSACTION_COST_CSV || "./data/evaluation/transaction_costs.csv";
const transactionCostCsvHeader = [
    "timestamp_unix_ms",
    "scope",
    "operation",
    "phase",
    "transactionHash",
    "blockNumber",
    "from",
    "to",
    "contractAddress",
    "gasUsed",
    "effectiveGasPriceWei",
    "effectiveGasPriceGwei",
    "costWei",
    "costGwei",
    "costEth",
    "costEur",
    "costUsd",
    "ethEurPrice",
    "ethUsdPrice",
    "feeBasis",
    "valuationKind",
    "exchangeRateSource",
    "exchangeRateTimestampUtc",
    "referenceGasPriceGwei",
    "referenceGasPriceSource",
    "referenceGasPriceTimestampUtc",
    "mainnetEstimateWei",
    "mainnetEstimateGwei",
    "mainnetEstimateEth",
    "mainnetEstimateEur",
    "mainnetEstimateUsd",
    "mainnetEstimateKind",
    "account",
    "deviceId",
];
const csvValue = (value) => {
    const text = value === undefined || value === null ? "" : String(value);
    return `"${text.replace(/"/g, '""')}"`;
};
const appendTransactionCostCsv = (event) => {
    try {
        const directory = transactionCostCsvPath.replace(/\/[^/]*$/, "");
        if (directory && directory !== transactionCostCsvPath) {
            fs.mkdirSync(directory, { recursive: true });
        }
        if (!fs.existsSync(transactionCostCsvPath)) {
            fs.appendFileSync(transactionCostCsvPath, `${transactionCostCsvHeader.join(",")}\n`);
        }
        const row = transactionCostCsvHeader.map((field) => csvValue(event[field] ?? "")).join(",");
        fs.appendFileSync(transactionCostCsvPath, `${row}\n`);
    }
    catch (error) {
        console.warn("Could not append transaction cost CSV:", error?.message || String(error));
    }
};
const logTransactionCost = (scope, operation, receipt, fallbackGasPriceWei) => {
    const gasUsed = BigInt(receipt?.gasUsed?.toString?.() ?? receipt?.gasUsed ?? 0);
    const effectiveGasPriceWei = BigInt(receipt?.effectiveGasPrice?.toString?.() ?? receipt?.effectiveGasPrice ?? fallbackGasPriceWei ?? 0);
    const amounts = transactionCostAmounts(gasUsed, effectiveGasPriceWei, ethEurPrice, ethUsdPrice);
    const mainnetAmounts = referenceGasPriceGwei
        ? transactionCostAmounts(gasUsed, gweiRateToWei(referenceGasPriceGwei), ethEurPrice, ethUsdPrice)
        : undefined;
    const event = {
        timestamp_unix_ms: Date.now(),
        kind: "gas_cost",
        scope,
        operation,
        phase: "",
        gasUsed: gasUsed.toString(),
        effectiveGasPriceWei: effectiveGasPriceWei.toString(),
        ...amounts,
        ethEurPrice: ethEurPrice.text,
        ethUsdPrice: ethUsdPrice?.text || "",
        feeBasis: "receipt.effectiveGasPrice",
        valuationKind: "receipt_fee_fiat_estimate",
        exchangeRateSource,
        exchangeRateTimestampUtc,
        referenceGasPriceGwei: referenceGasPriceGwei?.text || "",
        referenceGasPriceSource,
        referenceGasPriceTimestampUtc,
        mainnetEstimateWei: mainnetAmounts?.costWei || "",
        mainnetEstimateGwei: mainnetAmounts?.costGwei || "",
        mainnetEstimateEth: mainnetAmounts?.costEth || "",
        mainnetEstimateEur: mainnetAmounts?.costEur || "",
        mainnetEstimateUsd: mainnetAmounts?.costUsd || "",
        mainnetEstimateKind: mainnetAmounts ? "counterfactual_mainnet_equivalent" : "",
        transactionHash: receipt?.transactionHash,
        blockNumber: Number(receipt?.blockNumber?.toString?.() ?? receipt?.blockNumber ?? 0),
        from: receipt?.from,
        to: receipt?.to,
        contractAddress: receipt?.contractAddress,
        account: process.env.ACCOUNT_ADDRESS,
        deviceId: process.env.DEVICE_ID,
    };
    console.log(JSON.stringify(event, jsonReplacer));
    appendTransactionCostCsv(event);
    void emitTelemetryEvent("worker.transaction_cost", event);
};
const withGasBuffer = (gasEstimate, percent = 30n) => {
    const estimate = BigInt(gasEstimate);
    return ((estimate * (100n + percent)) + 99n) / 100n;
};
const protocolSigner = () => {
    if (!participantActionSigner) {
        throw new Error("Participant action signer has not been configured.");
    }
    return participantActionSigner;
};
const sendProtocolMethod = async (method, to, { gasBuffer = false, errorLabel = "Error sending participant action transaction", } = {}) => {
    const previousTransaction = protocolTransactionQueue;
    let releaseTransaction = () => { };
    protocolTransactionQueue = new Promise((resolve) => {
        releaseTransaction = resolve;
    });
    await previousTransaction;
    try {
        const signer = protocolSigner();
        const gasPrice = await web3.eth.getGasPrice();
        const gasEstimate = await method.estimateGas({ from: signer.address });
        const nonce = await web3.eth.getTransactionCount(signer.address, "pending");
        const gas = gasBuffer ? withGasBuffer(gasEstimate).toString() : gasEstimate;
        const tx = {
            from: signer.address,
            to,
            gas,
            gasPrice,
            nonce,
            chainId: expectedChainId().toString(),
            data: method.encodeABI(),
        };
        const rawTransaction = await signer.signTransaction(tx);
        const receipt = await web3.eth.sendSignedTransaction(rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Participant action transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error(`${errorLabel}:`, error);
        throw error;
    }
    finally {
        releaseTransaction();
    }
};
const bootstrapAccount = () => {
    if (!/^0x[0-9a-fA-F]{64}$/.test(String(bootstrapPrivateKey || ""))) {
        throw new Error("PRIVATE_KEY must contain the logical participant bootstrap key.");
    }
    return web3.eth.accounts.privateKeyToAccount(bootstrapPrivateKey);
};
const expectedChainId = () => {
    const configured = String(process.env.EXPECTED_CHAIN_ID || "").trim();
    if (!/^(?:0|[1-9][0-9]*)$/.test(configured)) {
        throw new Error("EXPECTED_CHAIN_ID must be configured as a decimal integer.");
    }
    const chainId = BigInt(configured);
    if (chainId <= 0n) {
        throw new Error("EXPECTED_CHAIN_ID must be positive.");
    }
    return chainId;
};
export const fundParticipantActionKey = async () => {
    const signer = protocolSigner();
    const account = bootstrapAccount();
    const logicalAddress = String(process.env.ACCOUNT_ADDRESS || "").toLowerCase();
    if (account.address.toLowerCase() !== logicalAddress) {
        throw new Error(`PRIVATE_KEY account ${account.address} does not match ACCOUNT_ADDRESS ${logicalAddress}`);
    }
    const minimum = BigInt(process.env.ACTION_KEY_MIN_BALANCE_WEI || "1000000000000000000");
    const target = BigInt(process.env.ACTION_KEY_TARGET_BALANCE_WEI || "10000000000000000000");
    const balance = BigInt(await web3.eth.getBalance(signer.address));
    if (balance >= minimum)
        return null;
    if (target <= balance) {
        throw new Error("ACTION_KEY_TARGET_BALANCE_WEI must exceed the current action-key balance.");
    }
    const gasPrice = await web3.eth.getGasPrice();
    const tx = {
        from: account.address,
        to: signer.address,
        value: (target - balance).toString(),
        gas: "21000",
        gasPrice,
        nonce: await web3.eth.getTransactionCount(account.address, "pending"),
        chainId: expectedChainId().toString(),
    };
    const signed = await web3.eth.accounts.signTransaction(tx, bootstrapPrivateKey);
    const receipt = await web3.eth.sendSignedTransaction(signed.rawTransaction);
    logTransactionCost("worker", "action_key_funding", receipt, gasPrice);
    console.log("Funded TEE participant action address.", {
        participant: logicalAddress,
        actionAddress: signer.address,
        targetBalanceWei: target.toString(),
    });
    return receipt;
};
const getGMStorageContract = () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    return new web3.eth.Contract(abi, address);
};
const getAggregationPolicyContract = async () => {
    const address = String(await getGMStorageContract().methods.aggregation_policy_address().call());
    if (!/^0x[0-9a-fA-F]{40}$/.test(address) || /^0x0{40}$/i.test(address)) {
        throw new Error(`GMStorage returned an invalid aggregation-policy address: ${address}`);
    }
    const abi = JSON.parse(fs.readFileSync("./abi/aggregation_policy.json", "utf-8"));
    return {
        address,
        contract: new web3.eth.Contract(abi, address),
    };
};
export const getBlockchainChainId = async () => Number(await web3.eth.getChainId());
const getAggregatorSelectionContract = () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    return new web3.eth.Contract(abi, aggregator_address);
};
const getAggregatorSelectionDiagnostics = async (contract, caller) => {
    const [state, round, lastRoundAggregator, topContributor, authorizedDevices, latestBlock] = await Promise.all([
        contract.methods.getSystemState().call().catch((error) => ({ error: serializeError(error) })),
        getRound().catch((error) => ({ error: serializeError(error) })),
        getLastRoundsAggregator().catch((error) => ({ error: serializeError(error) })),
        getTopContributor().catch((error) => ({ error: serializeError(error) })),
        getAuthorizedDevices().catch((error) => ({ error: serializeError(error) })),
        web3.eth.getBlock("latest").catch((error) => ({ error: serializeError(error) })),
    ]);
    const stateHasError = Boolean(state?.error);
    const blockHasError = Boolean(latestBlock?.error);
    return {
        caller,
        contract: aggregator_address,
        gmStorage: gm_storage_address,
        deviceRegistry: device_registry_address,
        state: stateHasError ? state : {
            systemState: state?.[0],
            currentAggregator: state?.[1],
            brokerEndpoint: state?.[2],
            timeToAggregate: state?.[3]?.toString?.() ?? state?.[3],
            timeToSelect: state?.[4]?.toString?.() ?? state?.[4],
        },
        round,
        lastRoundAggregator,
        topContributor,
        authorizedDeviceCount: Array.isArray(authorizedDevices) ? authorizedDevices.length : undefined,
        authorizedDevices,
        latestBlock: blockHasError ? latestBlock : {
            number: latestBlock?.number?.toString?.() ?? latestBlock?.number,
            timestamp: latestBlock?.timestamp?.toString?.() ?? latestBlock?.timestamp,
            prevrandao: latestBlock?.prevrandao,
            hash: latestBlock?.hash,
        },
    };
};
// send contract call to blockchain
export const getCurrentGM = async () => {
    const contract = getGMStorageContract();
    // call method of contract
    let ipfs_address = await contract.methods.getGlobalModel().call().then((result) => {
        return result;
    });
    return ipfs_address;
};
export const getCurrentGMSignature = async () => {
    const contract = getGMStorageContract();
    let sig = await contract.methods.getGlobalModelSignature().call().then((result) => {
        return result;
    });
    return sig;
};
export const getCurrentGMKeyBundle = async () => {
    const contract = getGMStorageContract();
    let cid = await contract.methods.getGlobalModelKeyBundle().call().then((result) => {
        return result;
    });
    return cid;
};
export const getActiveModelBundle = async () => {
    const contract = getGMStorageContract();
    // The legacy active-bundle view has no model-round output. Use the atomic
    // finalized view so the CID tuple, publisher key and authoritative model
    // round are obtained under the same contract invariants and eth_call.
    const result = await contract.methods.getFinalizedModelBundle().call();
    const modelRound = Number(result.modelRound ?? result[4]);
    if (!Number.isSafeInteger(modelRound) || modelRound <= 0) {
        throw new Error(`GMStorage returned an invalid finalized model round: ${modelRound}`);
    }
    return {
        modelCid: String(result.model ?? result[0] ?? ""),
        sigCid: String(result.signature ?? result[1] ?? ""),
        keyBundleCid: String(result.keyBundle ?? result[2] ?? ""),
        publisher: String(result.aggregator ?? result[3] ?? ""),
        modelRound,
        publisherPublicKeyDerHex: normalizePublisherPublicKeyDerHex(result.publisherPublicKey ?? result[5]),
    };
};
export const getLastRoundsAggregator = async () => {
    const contract = getGMStorageContract();
    let agg = await contract.methods.getLastRoundsAggregator().call().then((result) => {
        return result;
    });
    return agg;
};
// Backwards-compatible alias used by server.js
export const getPreviousAggregatorFromGMStorage = async () => {
    return await getLastRoundsAggregator();
};
export const getRoundAggregationPolicy = async (expectedRound) => {
    const { address, contract } = await getAggregationPolicyContract();
    const [result, latestBlock] = await Promise.all([
        contract.methods.getRoundPolicy(expectedRound).call(),
        web3.eth.getBlock("latest"),
    ]);
    const policy = {
        contractAddress: address,
        opened: Boolean(result.opened ?? result[0]),
        closed: Boolean(result.closed ?? result[1]),
        openedAt: Number(result.openedAt ?? result[2]),
        deadline: Number(result.deadline ?? result[3]),
        requiredSubmissions: Number(result.requiredSubmissions ?? result[4]),
        acceptedSubmissions: Number(result.acceptedSubmissions ?? result[5]),
        configurationVersion: Number(result.roundConfigurationVersion ?? result[6]),
        algorithmHash: String(result.algorithmHash ?? result[7]),
        validationDataHash: String(result.validationDataHash
            ?? result[8]
            ?? `0x${"00".repeat(32)}`),
        maxLossIncreaseBps: Number(result.maxLossIncreaseBps ?? result[9] ?? 0),
        policyHash: String(result.policyHash ?? result[10]),
        inputRoot: String(result.inputRoot ?? result[11]),
        chainTimestamp: Number(latestBlock.timestamp),
    };
    if (policy.opened) {
        const derivedPolicyHash = deriveRoundAggregationPolicyHash({
            configurationVersion: policy.configurationVersion,
            requiredSubmissions: policy.requiredSubmissions,
            openedAt: policy.openedAt,
            deadline: policy.deadline,
            algorithmHash: policy.algorithmHash,
            validationDataHash: policy.validationDataHash,
            maxLossIncreaseBps: policy.maxLossIncreaseBps,
        });
        if (derivedPolicyHash.toLowerCase() !== policy.policyHash.toLowerCase()) {
            throw new Error(`Aggregation policy hash mismatch for round ${expectedRound}: ` +
                `contract=${policy.policyHash}, derived=${derivedPolicyHash}.`);
        }
    }
    return policy;
};
export const openModelSubmissions = async (expectedRound) => {
    const current = await getRoundAggregationPolicy(expectedRound);
    if (current.opened) {
        return current;
    }
    const contract = getGMStorageContract();
    await sendProtocolMethod(contract.methods.openModelSubmissions(expectedRound), gm_storage_address, {
        gasBuffer: true,
        errorLabel: "Error opening the model-submission policy",
    });
    const opened = await getRoundAggregationPolicy(expectedRound);
    if (!opened.opened || opened.closed) {
        throw new Error(`Aggregation policy for round ${expectedRound} was not opened.`);
    }
    return opened;
};
export const isGlobalModelPublished = async (sourceRound) => {
    const published = await getGMStorageContract().methods
        .globalModelPublished(sourceRound)
        .call();
    return Boolean(published);
};
export const isRoundCompleted = async (sourceRound) => {
    const completed = await getGMStorageContract().methods
        .roundCompleted(sourceRound)
        .call();
    return Boolean(completed);
};
export const isRoundAborted = async (sourceRound) => {
    const aborted = await getGMStorageContract().methods
        .roundAborted(sourceRound)
        .call();
    return Boolean(aborted);
};
export const createAggregationStatement = async ({ sourceRound, modelCid, signatureCid, keyBundleCid, outputModelHash, outputBundleHash, }) => {
    const policy = await getRoundAggregationPolicy(sourceRound);
    if (!policy.opened || !policy.closed) {
        throw new Error(`Cannot sign aggregation statement for round ${sourceRound}: inputs are not closed.`);
    }
    if (policy.acceptedSubmissions < policy.requiredSubmissions) {
        throw new Error(`Aggregation policy for round ${sourceRound} requires ` +
            `${policy.requiredSubmissions} submissions but records ${policy.acceptedSubmissions}.`);
    }
    const state = await getCurrentState();
    const aggregator = String(state[1]);
    if (!sameAddress(aggregator, process.env.ACCOUNT_ADDRESS)) {
        throw new Error(`Current aggregator ${aggregator} does not match logical participant ` +
            `${process.env.ACCOUNT_ADDRESS}.`);
    }
    const signer = protocolSigner();
    const registeredActionKey = await getDeviceActionKey(aggregator);
    if (!sameAddress(registeredActionKey, signer.address)) {
        throw new Error(`Registered action key ${registeredActionKey} does not match live TEE action key ${signer.address}.`);
    }
    const { contract } = await getAggregationPolicyContract();
    const nonce = String(await contract.methods.aggregationNonces(aggregator).call());
    const publicationHash = derivePublicationHash({
        modelCid,
        signatureCid,
        keyBundleCid,
    });
    const localDigest = deriveAggregationStatementDigest({
        chainId: expectedChainId(),
        verifyingContract: gm_storage_address,
        round: sourceRound,
        aggregator,
        inputRoot: policy.inputRoot,
        inputCount: policy.acceptedSubmissions,
        algorithmHash: policy.algorithmHash,
        policyHash: policy.policyHash,
        outputModelHash,
        outputBundleHash,
        publicationHash,
        nonce,
    });
    const contractDigest = await contract.methods.aggregationStatementDigest(sourceRound, aggregator, policy.inputRoot, policy.acceptedSubmissions, policy.algorithmHash, policy.policyHash, outputModelHash, outputBundleHash, publicationHash, nonce).call();
    if (String(contractDigest).toLowerCase() !== localDigest.toLowerCase()) {
        throw new Error(`Local aggregation digest ${localDigest} does not match AggregationPolicy ${contractDigest}.`);
    }
    return {
        sourceRound: Number(sourceRound),
        aggregator,
        policy,
        publicationHash,
        outputModelHash,
        outputBundleHash,
        nonce,
        statementDigest: localDigest,
        statementSignature: await signer.signDigest(localDigest),
    };
};
export const finalizeRoundWithAggregation = async ({ modelCid, signatureCid, keyBundleCid, outputModelHash, outputBundleHash, statementSignature, }) => {
    const contract = getGMStorageContract();
    const receipt = await sendProtocolMethod(contract.methods.finalizeRoundWithAggregation(modelCid, signatureCid, keyBundleCid, outputModelHash, outputBundleHash, statementSignature), gm_storage_address, {
        gasBuffer: true,
        errorLabel: "Error atomically finalizing the aggregation round",
    });
    await logWorkerScore(process.env.ACCOUNT_ADDRESS, "aggregation");
    return receipt;
};
const logWorkerScore = async (deviceAddress, reason = "update") => {
    try {
        const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
        const contract = new web3.eth.Contract(abi, gm_storage_address);
        const score = await contract.methods.getContribution(deviceAddress).call();
        console.log(JSON.stringify({
            kind: "worker_score",
            account: deviceAddress,
            score: Number(score?.toString?.() ?? score ?? 0),
            reason,
        }, jsonReplacer));
    }
    catch (error) {
        console.error("Error reading worker score:", serializeError(error));
    }
};
export const createModelSubmissionCommitment = async (expectedRound, workerAddress, modelHash, packageHash) => {
    const contract = getGMStorageContract();
    const [parentModelHash, workerNonce, state] = await Promise.all([
        contract.methods.currentParentModelHash().call(),
        contract.methods.workerSubmissionNonces(workerAddress).call(),
        getCurrentState(),
    ]);
    const aggregatorAddress = String(state[1]);
    const localDigest = deriveModelSubmissionDigest({
        chainId: expectedChainId(),
        verifyingContract: gm_storage_address,
        round: expectedRound,
        worker: workerAddress,
        aggregator: aggregatorAddress,
        parentModelHash,
        modelHash,
        packageHash,
        nonce: workerNonce,
    });
    // An eth_call is useful as a compatibility check, but its return value is
    // never signed. RPC_URL may be routed through infrastructure outside the
    // worker TEE and therefore must not become a raw action-key signing oracle.
    const contractDigest = await contract.methods.modelSubmissionDigest(expectedRound, workerAddress, aggregatorAddress, parentModelHash, modelHash, packageHash, workerNonce).call();
    if (String(contractDigest).toLowerCase() !== localDigest.toLowerCase()) {
        throw new Error(`Local model-submission digest ${localDigest} does not match GMStorage ${contractDigest}.`);
    }
    const signer = protocolSigner();
    const registeredActionKey = await getDeviceActionKey(workerAddress);
    if (String(registeredActionKey).toLowerCase() !== signer.address.toLowerCase()) {
        throw new Error(`Registered action key ${registeredActionKey} does not match live TEE action key ${signer.address}.`);
    }
    return {
        expectedRound: Number(expectedRound),
        workerAddress,
        aggregatorAddress,
        parentModelHash,
        modelHash,
        packageHash,
        workerNonce: String(workerNonce),
        workerSignature: await signer.signDigest(localDigest),
    };
};
export const recordModelSubmission = async (commitment) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const method = contract.methods.recordModelSubmission(commitment.expectedRound, commitment.workerAddress, commitment.modelHash, commitment.packageHash, commitment.parentModelHash, commitment.workerNonce, commitment.workerSignature);
    const receipt = await sendProtocolMethod(method, address, { gasBuffer: true });
    await logWorkerScore(commitment.workerAddress, "model_submission");
    return receipt;
};
export const closeModelSubmissions = async (expectedRound) => {
    const contract = getGMStorageContract();
    if (await contract.methods.modelSubmissionsClosed(expectedRound).call()) {
        return null;
    }
    const method = contract.methods.closeModelSubmissions(expectedRound);
    return sendProtocolMethod(method, gm_storage_address, {
        gasBuffer: true,
        errorLabel: "Error closing the model-submission window",
    });
};
export const hasSubmittedModel = async (round, address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, gm_storage_address);
    const result = await contract.methods.hasSubmittedModel(round, address).call();
    return result;
};
export const getModelSubmissionHash = async (round, address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, gm_storage_address);
    return contract.methods.modelSubmissionHash(round, address).call();
};
export const getTopContributor = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    let topContributor = await contract.methods.getTopContributor().call().then((result) => {
        return result;
    });
    return topContributor;
};
export const getRound = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    let round = await contract.methods.getRound().call().then((result) => {
        return result;
    });
    return round;
};
export const getCompletedRoundCount = async () => {
    const contract = getGMStorageContract();
    const count = await contract.methods.getCompletedRoundCount().call();
    return Number(count);
};
export const getLastSelectionRound = async () => {
    const contract = getAggregatorSelectionContract();
    return Number(await contract.methods.lastSelectionRound().call());
};
// get current state from aggregator
export const getCurrentState = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    let state = await contract.methods.getSystemState().call().then((result) => {
        return result;
    });
    return state;
};
export const setCurrentState = async (newState) => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    return sendProtocolMethod(contract.methods.setSystemState(newState), address);
};
/* ------------------- Helper functions for state contract ------------------ */
export const getAggregatorEndpoint = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    let endpoint = await contract.methods.getBrokerEndpoint().call().then((result) => {
        return result;
    });
    return endpoint;
};
export const setAggregatorEndpoint = async (newEndpoint) => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    return sendProtocolMethod(contract.methods.setBrokerEndpoint(newEndpoint), address);
};
export const triggerAggregatorSelection = async () => {
    const address = aggregator_address;
    const contract = getAggregatorSelectionContract();
    const signer = protocolSigner();
    logJson("[aggregator-selection] preflight diagnostics", await getAggregatorSelectionDiagnostics(contract, signer.address));
    const method = contract.methods.triggerAggregatorSelection();
    try {
        await method.call({ from: signer.address });
        console.log("[aggregator-selection] eth_call simulation succeeded");
    }
    catch (error) {
        console.error("[aggregator-selection] eth_call simulation failed:", serializeError(error));
        throw error;
    }
    const gasPrice = await web3.eth.getGasPrice();
    let gasEstimate;
    try {
        gasEstimate = await method.estimateGas({ from: signer.address });
        console.log("[aggregator-selection] gas estimate:", gasEstimate.toString(), "gasPrice:", gasPrice.toString());
    }
    catch (error) {
        console.error("[aggregator-selection] gas estimate failed:", serializeError(error));
        throw error;
    }
    const gasLimit = withGasBuffer(gasEstimate);
    console.log("[aggregator-selection] gas limit with buffer:", gasLimit.toString());
    const tx = {
        from: signer.address,
        to: address,
        gas: gasLimit.toString(),
        gasPrice: gasPrice,
        nonce: await web3.eth.getTransactionCount(signer.address, "pending"),
        chainId: expectedChainId().toString(),
        data: method.encodeABI(),
    };
    try {
        const rawTransaction = await signer.signTransaction(tx);
        const receipt = await web3.eth.sendSignedTransaction(rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        logJson("[aggregator-selection] post-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, signer.address));
        return receipt;
    }
    catch (error) {
        console.error("[aggregator-selection] transaction failed:", serializeError(error));
        logJson("[aggregator-selection] failed-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, signer.address));
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
export const reportAggregatorTimeout = async (expectedRound, expectedAggregator) => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    const signer = protocolSigner();
    const reportedRound = Number(expectedRound);
    const reportedAggregator = String(expectedAggregator || "").toLowerCase();
    if (!Number.isSafeInteger(reportedRound) || reportedRound < 0) {
        throw new Error(`Invalid observed timeout round: ${expectedRound}`);
    }
    if (!/^0x[0-9a-f]{40}$/.test(reportedAggregator)) {
        throw new Error(`Invalid observed timeout aggregator: ${expectedAggregator}`);
    }
    const gasPrice = await web3.eth.getGasPrice();
    const method = contract.methods.reportAggregatorTimeout(reportedRound, reportedAggregator);
    const gasEstimate = await method.estimateGas({ from: signer.address });
    const tx = {
        from: signer.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce: await web3.eth.getTransactionCount(signer.address, "pending"),
        chainId: expectedChainId().toString(),
        data: method.encodeABI(),
    };
    try {
        const rawTransaction = await signer.signTransaction(tx);
        const receipt = await web3.eth.sendSignedTransaction(rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        const currentAggregator = await contract.methods.getCurrentAggregator().call();
        if (String(currentAggregator).toLowerCase() !== String(reportedAggregator).toLowerCase()) {
            await logWorkerScore(reportedAggregator, "aggregator_timeout_consensus");
        }
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
// check if an address is authorized
export const isAuthorized = async (address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await contract.methods.isAuthorized(address).call();
    return result;
};
export const getAuthorizedDevices = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await contract.methods.getAuthorizedDevices().call();
    return Array.from(result || []);
};
export const getCommittedRunRosterState = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const blockNumber = await web3.eth.getBlockNumber();
    const callAtSnapshot = (method) => method.call({}, blockNumber);
    const normalizeBoolean = (value, label) => {
        if (value === true || value === "true" || value === 1 || value === "1") {
            return true;
        }
        if (value === false || value === "false" || value === 0 || value === "0") {
            return false;
        }
        throw new Error(`DeviceRegistry returned an invalid ${label}: ${value}`);
    };
    const [committedRaw, frozenRaw, digest, registeredWorkerCount, roster] = await Promise.all([
        callAtSnapshot(contract.methods.runRosterCommitted()),
        callAtSnapshot(contract.methods.runRosterFrozen()),
        callAtSnapshot(contract.methods.runRosterDigest()),
        callAtSnapshot(contract.methods.registeredRunMemberCount()),
        callAtSnapshot(contract.methods.getRunRoster()),
    ]);
    const committed = normalizeBoolean(committedRaw, "run-roster commitment flag");
    if (!committed) {
        return {
            committed: false,
            frozen: false,
            digest: null,
            registeredWorkerCount: 0,
            roster: [],
        };
    }
    return {
        committed: true,
        frozen: normalizeBoolean(frozenRaw, "run-roster frozen flag"),
        digest: String(digest || "").toLowerCase(),
        registeredWorkerCount: Number(registeredWorkerCount),
        roster: Array.from(roster || []).map((address) => String(address || "").toLowerCase()),
    };
};
const decodeBytes32Text = (value) => {
    if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value)) {
        throw new Error(`Invalid bytes32 signer id: ${value}`);
    }
    return Buffer.from(value.slice(2), "hex").toString("utf8").replace(/\0+$/g, "");
};
export const getMedicalSignerSnapshot = async () => {
    if (!medical_signer_registry_address) {
        throw new Error("MEDICAL_SIGNER_REGISTRY_ADDRESS is required for signed ChestMNIST training data");
    }
    const abi = JSON.parse(fs.readFileSync("./abi/medical_signer_registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, medical_signer_registry_address);
    const blockNumber = await web3.eth.getBlockNumber();
    const block = await web3.eth.getBlock(blockNumber);
    const callAtSnapshot = (method) => method.call({}, blockNumber);
    const [keySetVersion, deviceIds, radiologistIds] = await Promise.all([
        callAtSnapshot(contract.methods.keySetVersion()),
        callAtSnapshot(contract.methods.getActiveSignerIds(1)),
        callAtSnapshot(contract.methods.getActiveSignerIds(2)),
    ]);
    const signerEntries = await Promise.all([
        ...Array.from(deviceIds || []).map((id) => ({ id, expectedRole: 1, roleName: "XRAY_DEVICE" })),
        ...Array.from(radiologistIds || []).map((id) => ({ id, expectedRole: 2, roleName: "RADIOLOGIST" })),
    ].map(async ({ id, expectedRole, roleName }) => {
        const result = await callAtSnapshot(contract.methods.getSigner(id));
        const actualRole = Number((result.role ?? result[1])?.toString?.() ?? result.role ?? result[1]);
        if (actualRole !== expectedRole) {
            throw new Error(`Medical signer ${id} changed role inside block snapshot`);
        }
        const activeValue = result.active ?? result[0];
        return {
            signer_id: decodeBytes32Text(String(id)),
            active: activeValue === true || activeValue === "true",
            role: roleName,
            display_name: String(result.displayName ?? result[2]),
            public_key_der_hex: String(result.publicKeyDer ?? result[3]),
            certificate_der_hex: String(result.certificateDer ?? result[4]),
            certificate_fingerprint: String(result.certificateFingerprint ?? result[5]),
        };
    }));
    return {
        registry_address: medical_signer_registry_address,
        block_number: Number(blockNumber),
        block_hash: String(block?.hash || ""),
        key_set_version: String(keySetVersion?.toString?.() ?? keySetVersion),
        signers: signerEntries,
    };
};
// get device public key (bytes) from registry by device address
export const getDevicePublicKey = async (address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await contract.methods.getDevice(address).call();
    // web3 may return both array indices and named fields
    const publicKey = (result && (result.public_key ?? result[3])) ?? "0x";
    return publicKey;
};
export const getDeviceActionKey = async (address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    return String(await contract.methods.actionKeys(address).call());
};
export const isDeviceRegistrationCurrent = async (address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey, canonicalAppCompose) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    return Boolean(await contract.methods
        .isDeviceRegistrationCurrent(address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey, canonicalAppCompose)
        .call());
};
export const getDeviceRegistrationReportData = async (address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey, canonicalAppCompose) => {
    if (!device_registry_address) {
        throw new Error("REGISTRY_ADDRESS is required for TDX device registration");
    }
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const reportData = await contract.methods.registrationReportData(address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey, canonicalAppCompose).call();
    if (typeof reportData !== "string" || !/^0x[0-9a-fA-F]{128}$/.test(reportData)) {
        throw new Error("DeviceRegistry returned invalid registration REPORTDATA");
    }
    return reportData;
};
export const registerDeviceWithTeeQuoteAndRtmr3Events = async (quoteHex, rtmr3EventLog, canonicalAppCompose, address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey) => {
    if (!device_registry_address) {
        throw new Error("REGISTRY_ADDRESS is required for TDX device registration");
    }
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = bootstrapAccount();
    if (account.address.toLowerCase() !== String(address || "").toLowerCase()) {
        throw new Error(`PRIVATE_KEY account ${account.address} does not match ACCOUNT_ADDRESS ${address}`);
    }
    const signer = protocolSigner();
    if (signer.address.toLowerCase() !== String(actionKey || "").toLowerCase()) {
        throw new Error(`Live TEE action key ${signer.address} does not match requested action key ${actionKey}`);
    }
    const enrollmentDigest = await contract.methods.enrollmentDigest(address, actionKey, selloReceiptKey, canonicalAppCompose).call();
    const participantAuthorization = signRawDigest(enrollmentDigest, bootstrapPrivateKey);
    const gasPrice = await web3.eth.getGasPrice();
    const registration = contract.methods.registerDeviceWithAttestedAppCompose(quoteHex, rtmr3EventLog, canonicalAppCompose, address, actionKey, publicIp, brokerIp, publicKeyBytesHex, selloReceiptKey, participantAuthorization);
    const calldata = registration.encodeABI();
    const hexByteLength = (value) => {
        if (typeof value !== "string" || !/^0x[0-9a-fA-F]*$/.test(value) || value.length % 2 !== 0) {
            throw new Error("Invalid hex value in TDX registration payload");
        }
        return (value.length - 2) / 2;
    };
    console.log("TDX registration payload:", {
        quoteBytes: hexByteLength(quoteHex),
        rtmr3Events: rtmr3EventLog.length,
        appComposeBytes: hexByteLength(canonicalAppCompose),
        publicKeyBytes: hexByteLength(publicKeyBytesHex),
        calldataBytes: hexByteLength(calldata),
    });
    if (process.env.LOCAL_TDX_MOCK !== "1") {
        try {
            const attestationAddress = await contract.methods.tdxV4Attestation().call();
            const debugAbi = [{
                    type: "function",
                    name: "debugVerifyWithRtmr3EventLog",
                    inputs: [
                        { name: "input", type: "bytes" },
                        {
                            name: "rtmr3EventLog",
                            type: "tuple[]",
                            components: [
                                { name: "eventType", type: "uint32" },
                                { name: "eventName", type: "string" },
                                { name: "eventPayload", type: "bytes" },
                            ],
                        },
                        { name: "composeHash", type: "bytes32" },
                    ],
                    outputs: [
                        { name: "stage", type: "uint8" },
                        { name: "qeTcbStatus", type: "uint8" },
                        { name: "tcbStatus", type: "uint8" },
                        { name: "pcesvn", type: "uint16" },
                        { name: "fmspc", type: "bytes6" },
                        { name: "teeTcbSvn", type: "bytes16" },
                        { name: "qeIsvProdId", type: "uint16" },
                        { name: "qeIsvSvn", type: "uint16" },
                    ],
                    stateMutability: "view",
                }];
            const attestation = new web3.eth.Contract(debugAbi, attestationAddress);
            const identity = await contract.methods.workloadIdentity(canonicalAppCompose).call();
            const composeHash = identity.composeHash ?? identity[0];
            const debugResult = await attestation.methods
                .debugVerifyWithRtmr3EventLog(quoteHex, rtmr3EventLog, composeHash)
                .call({ from: signer.address });
            console.log("TDX debug verification:", {
                attestationAddress,
                stage: String(debugResult.stage ?? debugResult[0]),
                qeTcbStatus: String(debugResult.qeTcbStatus ?? debugResult[1]),
                tcbStatus: String(debugResult.tcbStatus ?? debugResult[2]),
                pcesvn: String(debugResult.pcesvn ?? debugResult[3]),
                fmspc: String(debugResult.fmspc ?? debugResult[4]),
                teeTcbSvn: String(debugResult.teeTcbSvn ?? debugResult[5]),
                qeIsvProdId: String(debugResult.qeIsvProdId ?? debugResult[6]),
                qeIsvSvn: String(debugResult.qeIsvSvn ?? debugResult[7]),
            });
        }
        catch (error) {
            console.error("TDX debug verification call failed:", serializeError(error));
        }
    }
    const gasEstimate = await registration.estimateGas({ from: signer.address });
    const tx = {
        from: signer.address,
        to: device_registry_address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce: await web3.eth.getTransactionCount(signer.address, "pending"),
        chainId: expectedChainId().toString(),
        data: calldata,
    };
    try {
        const rawTransaction = await signer.signTransaction(tx);
        const receipt = await web3.eth.sendSignedTransaction(rawTransaction);
        logTransactionCost("worker", "rtmr3_registration", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending TDX RTMR3 registration transaction: ", error);
        throw error;
    }
};
export const leaveDeviceRegistry = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    return sendProtocolMethod(contract.methods.leaveNetwork(), device_registry_address, { gasBuffer: true });
};
