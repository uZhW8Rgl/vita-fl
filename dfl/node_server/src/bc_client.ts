// @ts-nocheck
import Web3 from "web3";
import fs from "fs";
import 'dotenv/config';
// setup client´
//const web3 = new Web3("https://eth-sepolia.g.alchemy.com/v2/pFowzUSGYob62Q7i2YVsF0LFUX3WiCT2");
const parseRpcUrls = () => {
    const rawValues = [
        process.env.SEPOLIA_RPC_URLS,
        process.env.RPC_URLS,
        process.env.SEPOLIA_RPC_URL,
        process.env.RPC_URL,
    ]
        .filter(Boolean)
        .flatMap((value) => String(value).split(","))
        .map((value) => value.trim())
        .filter(Boolean);

    return Array.from(new Set(rawValues));
};

const rpcUrls = parseRpcUrls();
if (rpcUrls.length === 0) {
    throw new Error("Missing RPC_URL or RPC_URLS environment configuration");
}

let currentRpcUrlIndex = 0;
let currentRpcUrl = rpcUrls[currentRpcUrlIndex];
const web3 = new Web3(currentRpcUrl);
// define smart contract addresses
const gm_storage_address = process.env.GM_STORAGE_ADDRESS;
const aggregator_address = process.env.AGGREGATOR_ADDRESS;
const device_registry_address = process.env.REGISTRY_ADDRESS;
const privateKey = process.env.PRIVATE_KEY;

const addAccountToWallet = (account) => {
    const address = account.address.toLowerCase();
    for (let i = 0; i < web3.eth.accounts.wallet.length; i++) {
        const walletAccount = web3.eth.accounts.wallet[i];
        if (walletAccount?.address?.toLowerCase() === address) {
            return;
        }
    }
    web3.eth.accounts.wallet.add(account);
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

const logJson = (label, payload) => {
    console.log(label, JSON.stringify(payload, jsonReplacer));
};

const ethEurPrice = Number(process.env.ETH_EUR_PRICE || "3000");
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
    "costEth",
    "costEur",
    "ethEurPrice",
    "account",
    "deviceId",
];

const rpcMaxAttempts = Number(process.env.RPC_MAX_ATTEMPTS || "6");
const rpcBaseDelayMs = Number(process.env.RPC_RETRY_BASE_DELAY_MS || "1500");
const rpcMaxDelayMs = Number(process.env.RPC_RETRY_MAX_DELAY_MS || "12000");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const isRateLimitError = (error) => {
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) =>
        value.includes("rate limit")
        || value.includes("too many requests")
        || value.includes("429")
    );
};

const isTransientRpcError = (error) => {
    if (error?.code === 100) {
        return true;
    }
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
        error?.data,
        error?.request,
        error?.name,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) =>
        value === "returned error:"
        || value.includes("returned error:")
        || value.includes("header not found")
        || value.includes("timeout")
        || value.includes("socket hang up")
        || value.includes("econnreset")
        || value.includes("etimedout")
        || value.includes("failed to fetch")
    );
};

const isAlreadyKnownError = (error) => {
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) => value.includes("already known"));
};

const isReplacementUnderpricedError = (error) => {
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) => value.includes("replacement transaction underpriced"));
};

const isTransactionNotFoundError = (error) => {
    if (error?.code === 430) {
        return true;
    }
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
        error?.data,
        error?.name,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) =>
        value.includes("transaction not found")
        || value.includes("transactionnotfound")
    );
};

const isTransactionBlockTimeoutError = (error) => {
    if (error?.code === 432) {
        return true;
    }
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
        error?.data,
        error?.name,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) =>
        value.includes("transactionblocktimeouterror")
        || value.includes("was not mined within")
        || value.includes("not mined within")
    );
};

const rotateRpcProvider = (reason, label) => {
    if (rpcUrls.length <= 1) {
        return false;
    }
    currentRpcUrlIndex = (currentRpcUrlIndex + 1) % rpcUrls.length;
    currentRpcUrl = rpcUrls[currentRpcUrlIndex];
    web3.setProvider(currentRpcUrl);
    console.warn(`[rpc-rotate] ${label} switched provider after ${reason}; now using ${currentRpcUrl}`);
    return true;
};

const withRpcRetry = async (label, fn, { attempts = rpcMaxAttempts } = {}) => {
    let lastError;
    for (let attempt = 1; attempt <= attempts; attempt++) {
        try {
            return await fn();
        } catch (error) {
            lastError = error;
            if ((!isRateLimitError(error) && !isTransientRpcError(error)) || attempt >= attempts) {
                throw error;
            }
            const delayMs = Math.min(rpcBaseDelayMs * (2 ** (attempt - 1)), rpcMaxDelayMs);
            const reason = isRateLimitError(error) ? "rate limit" : "transient RPC error";
            rotateRpcProvider(reason, label);
            console.warn(`[rpc-retry] ${label} hit ${reason} on attempt ${attempt}/${attempts}; retrying in ${delayMs}ms`);
            await sleep(delayMs);
        }
    }
    throw lastError;
};

const callMethod = async (method, options = {}, label = "contract.call") => {
    return await withRpcRetry(label, () => method.call(options));
};

const getGasPriceWithRetry = async (label = "eth_getGasPrice") => {
    const [nodeGasPriceRaw, latestBlock] = await Promise.all([
        withRpcRetry(label, () => web3.eth.getGasPrice()),
        withRpcRetry(`${label}.latestBlock`, () => web3.eth.getBlock("latest")),
    ]);
    const nodeGasPrice = BigInt(nodeGasPriceRaw?.toString?.() ?? nodeGasPriceRaw ?? 0);
    const baseFeePerGas = BigInt(latestBlock?.baseFeePerGas?.toString?.() ?? latestBlock?.baseFeePerGas ?? 0);
    if (baseFeePerGas <= 0n) {
        return nodeGasPrice.toString();
    }
    const paddedBaseFee = ((baseFeePerGas * 120n) + 99n) / 100n;
    const paddedNodeGasPrice = ((nodeGasPrice * 115n) + 99n) / 100n;
    return (paddedBaseFee > paddedNodeGasPrice ? paddedBaseFee : paddedNodeGasPrice).toString();
};

const getTransactionCountWithRetry = async (address, blockTag = "pending", label = "eth_getTransactionCount") => {
    const nonce = await withRpcRetry(label, () => web3.eth.getTransactionCount(address, blockTag));
    return nonce?.toString?.() ?? String(nonce);
};

const estimateGasWithRetry = async (method, options = {}, label = "eth_estimateGas") => {
    return await withRpcRetry(label, () => method.estimateGas(options));
};

const waitForTransactionReceipt = async (transactionHash, label = "eth_getTransactionReceipt") => {
    for (let attempt = 1; attempt <= rpcMaxAttempts; attempt++) {
        let receipt = null;
        try {
            receipt = await withRpcRetry(label, () => web3.eth.getTransactionReceipt(transactionHash));
        } catch (error) {
            if (!isTransactionNotFoundError(error)) {
                throw error;
            }
            console.warn(
                `[rpc-retry] ${label} has no receipt yet for ${transactionHash} on attempt ${attempt}/${rpcMaxAttempts}; retrying`
            );
        }
        if (receipt) {
            return receipt;
        }
        await sleep(Math.min(rpcBaseDelayMs * attempt, rpcMaxDelayMs));
    }
    throw new Error(`Timed out waiting for receipt for transaction ${transactionHash}`);
};

const waitForSuccessCondition = async (checkFn, label = "success_condition", attempts = rpcMaxAttempts) => {
    for (let attempt = 1; attempt <= attempts; attempt++) {
        const result = await withRpcRetry(label, () => checkFn());
        if (result) {
            return true;
        }
        await sleep(Math.min(rpcBaseDelayMs * attempt, rpcMaxDelayMs));
    }
    return false;
};

const waitForTransactionReceiptByHash = async (
    transactionHash,
    {
        receiptLabel = "eth_getTransactionReceipt",
        attempts = rpcMaxAttempts,
        pollBaseDelayMs = rpcBaseDelayMs,
        pollMaxDelayMs = rpcMaxDelayMs,
    } = {},
) => {
    for (let attempt = 1; attempt <= attempts; attempt++) {
        const receipt = await withRpcRetry(receiptLabel, () => web3.eth.getTransactionReceipt(transactionHash));
        if (receipt) {
            return receipt;
        }
        const delayMs = Math.min(pollBaseDelayMs * attempt, pollMaxDelayMs);
        console.warn(`[rpc-retry] ${receiptLabel} pending for ${transactionHash} on attempt ${attempt}/${attempts}; retrying in ${delayMs}ms`);
        await sleep(delayMs);
    }
    throw new Error(`Timed out waiting for receipt for transaction ${transactionHash}`);
};

const sendSignedTransactionWithRetry = async (rawTransaction, label = "eth_sendRawTransaction", transactionHash) => {
    try {
        return await withRpcRetry(label, async () => {
            let observedHash = transactionHash || null;
            const promiEvent = web3.eth.sendSignedTransaction(rawTransaction);
            return await new Promise((resolve, reject) => {
                let settled = false;

                const settle = (fn) => (value) => {
                    if (settled) return;
                    settled = true;
                    fn(value);
                };

                promiEvent.once("transactionHash", (hash) => {
                    observedHash = hash;
                    console.log(`[tx-send] ${label} broadcasted ${hash}`);
                    setTimeout(async () => {
                        if (settled || !observedHash) return;
                        try {
                            const receipt = await waitForTransactionReceiptByHash(observedHash, {
                                receiptLabel: `${label}.receiptFallback`,
                                attempts: Math.max(rpcMaxAttempts + 4, 10),
                            });
                            settle(resolve)(receipt);
                        } catch (error) {
                            settle(reject)(error);
                        }
                    }, rpcBaseDelayMs);
                });

                promiEvent.once("receipt", settle(resolve));
                promiEvent.once("error", async (error) => {
                    if (observedHash && (isAlreadyKnownError(error) || isTransactionNotFoundError(error) || isTransactionBlockTimeoutError(error))) {
                        try {
                            const receipt = await waitForTransactionReceiptByHash(observedHash, {
                                receiptLabel: `${label}.broadcastedReceipt`,
                                attempts: Math.max(rpcMaxAttempts + 16, 24),
                                pollBaseDelayMs: Math.max(rpcBaseDelayMs, 3000),
                                pollMaxDelayMs: Math.max(rpcMaxDelayMs, 30000),
                            });
                            settle(resolve)(receipt);
                            return;
                        } catch (receiptError) {
                            settle(reject)(receiptError);
                            return;
                        }
                    }
                    settle(reject)(error);
                });
            });
        });
    } catch (error) {
        if (transactionHash && (isAlreadyKnownError(error) || isTransactionBlockTimeoutError(error))) {
            console.warn(`[rpc-retry] ${label} has broadcasted ${transactionHash} but receipt is delayed; waiting for receipt`);
            return await waitForTransactionReceiptByHash(transactionHash, {
                receiptLabel: `${label}.getTransactionReceipt`,
                attempts: Math.max(rpcMaxAttempts + 16, 24),
                pollBaseDelayMs: Math.max(rpcBaseDelayMs, 3000),
                pollMaxDelayMs: Math.max(rpcMaxDelayMs, 30000),
            });
        }
        throw error;
    }
};

const readContractValue = async (method, label) => {
    return await callMethod(method, {}, label);
};

const signAndSendContractTx = async ({
    account,
    to,
    method,
    gasPriceLabel,
    gasEstimateLabel,
    sendLabel,
    gasBufferPercent = 0n,
}) => {
    const gasPrice = await getGasPriceWithRetry(gasPriceLabel);
    const gasEstimate = await estimateGasWithRetry(method, { from: account.address }, gasEstimateLabel);
    const nonce = await getTransactionCountWithRetry(account.address, "pending", `${sendLabel}.getTransactionCount`);
    const gasLimit = gasBufferPercent > 0n ? withGasBuffer(gasEstimate, gasBufferPercent) : BigInt(gasEstimate);
    const tx = {
        from: account.address,
        to,
        gas: gasLimit.toString(),
        gasPrice,
        nonce,
        data: method.encodeABI(),
    };
    const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
    const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, sendLabel, signedTx.transactionHash);
    return { receipt, gasPrice };
};

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
    } catch (error) {
        console.warn("Could not append transaction cost CSV:", error?.message || String(error));
    }
};

const logTransactionCost = (scope, operation, receipt, fallbackGasPriceWei) => {
    const gasUsed = BigInt(receipt?.gasUsed?.toString?.() ?? receipt?.gasUsed ?? 0);
    const effectiveGasPriceWei = BigInt(
        receipt?.effectiveGasPrice?.toString?.() ?? receipt?.effectiveGasPrice ?? fallbackGasPriceWei ?? 0
    );
    const costWei = gasUsed * effectiveGasPriceWei;
    const costEth = Number(costWei) / 1e18;
    const event = {
        timestamp_unix_ms: Date.now(),
        kind: "gas_cost",
        scope,
        operation,
        phase: "",
        gasUsed: Number(gasUsed),
        effectiveGasPriceWei: Number(effectiveGasPriceWei),
        effectiveGasPriceGwei: Number(effectiveGasPriceWei) / 1e9,
        costEth,
        costEur: costEth * ethEurPrice,
        ethEurPrice,
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
};

const withGasBuffer = (gasEstimate, percent = 30n) => {
    const estimate = BigInt(gasEstimate);
    return ((estimate * (100n + percent)) + 99n) / 100n;
};

const getGMStorageContract = () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    return new web3.eth.Contract(abi, address);
};

const getAggregatorSelectionContract = () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    return new web3.eth.Contract(abi, aggregator_address);
};

const getAggregatorSelectionDiagnostics = async (contract, caller) => {
    const [state, round, lastRoundAggregator, topContributor, authorizedDevices, latestBlock] =
        await Promise.all([
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
    return await readContractValue(contract.methods.getGlobalModel(), "gm.getGlobalModel");
};

export const getCurrentGMSignature = async () => {
    const contract = getGMStorageContract();
    return await readContractValue(contract.methods.getGlobalModelSignature(), "gm.getGlobalModelSignature");
};

export const getCurrentGMKeyBundle = async () => {
    const contract = getGMStorageContract();
    return await readContractValue(contract.methods.getGlobalModelKeyBundle(), "gm.getGlobalModelKeyBundle");
};

export const getLastRoundsAggregator = async () => {
    const contract = getGMStorageContract();
    return await readContractValue(contract.methods.getLastRoundsAggregator(), "gm.getLastRoundsAggregator");
};

// Backwards-compatible alias used by server.js
export const getPreviousAggregatorFromGMStorage = async () => {
    return await getLastRoundsAggregator();
};


// function to set global model
export const setGlobalModel = async (newIpfsAddress) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setGlobalModel(newIpfsAddress).estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.setGlobalModel.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.setGlobalModel(newIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.setGlobalModel.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.warn("[registry.registerDevice] send error diagnostics:", serializeError(error));
        if (isReplacementUnderpricedError(error) || isTransactionNotFoundError(error)) {
            console.warn("Registration transaction may already be pending onchain; waiting for authorization state");
            const authorized = await waitForSuccessCondition(
                () => isAuthorized(address),
                "registry.registerDevice.waitForAuthorization",
            );
            if (authorized) {
                console.log("Device registration completed via an existing pending transaction.");
                return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
            }
        }
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const setGlobalModelSignature = async (newSigIpfsAddress) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setGlobalModelSignature(newSigIpfsAddress).estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.setGlobalModelSignature.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.setGlobalModelSignature(newSigIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.setGlobalModelSignature.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const setGlobalModelAndSignature = async (newModelIpfsAddress, newSigIpfsAddress) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setGlobalModelAndSignature(newModelIpfsAddress, newSigIpfsAddress).estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.setGlobalModelAndSignature.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.setGlobalModelAndSignature(newModelIpfsAddress, newSigIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.setGlobalModelAndSignature.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const setGlobalModelAndSignatureAndKeyBundle = async (
    newModelIpfsAddress,
    newSigIpfsAddress,
    newKeyBundleIpfsAddress,
) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const method = contract.methods
        .setGlobalModelAndSignatureAndKeyBundle(
            newModelIpfsAddress,
            newSigIpfsAddress,
            newKeyBundleIpfsAddress,
        );
    try {
        let receipt;
        let gasPrice;
        try {
            const txResult = await signAndSendContractTx({
                account,
                to: address,
                method,
                gasPriceLabel: "gm.setGlobalModelAndSignatureAndKeyBundle.getGasPrice",
                gasEstimateLabel: "gm.setGlobalModelAndSignatureAndKeyBundle.estimateGas",
                sendLabel: "gm.setGlobalModelAndSignatureAndKeyBundle.send",
                gasBufferPercent: 80n,
            });
            receipt = txResult.receipt;
            gasPrice = txResult.gasPrice;
        } catch (error) {
            gasPrice = await getGasPriceWithRetry("gm.setGlobalModelAndSignatureAndKeyBundle.getGasPrice.fallback");
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error) && !isTransactionBlockTimeoutError(error)) {
                throw error;
            }
            const pendingHash = error?.receipt?.transactionHash
                || error?.transactionHash
                || error?.cause?.receipt?.transactionHash
                || error?.cause?.transactionHash;
            console.warn("GM bundle update transaction may already be pending onchain; waiting for updated on-chain CIDs");
            const gmUpdated = await waitForSuccessCondition(
                async () => {
                    const [latestModelCid, latestSigCid, latestKeyBundleCid] = await Promise.all([
                        getCurrentGM(),
                        getCurrentGMSignature(),
                        getCurrentGMKeyBundle(),
                    ]);
                    return String(latestModelCid || "") === String(newModelIpfsAddress)
                        && String(latestSigCid || "") === String(newSigIpfsAddress)
                        && String(latestKeyBundleCid || "") === String(newKeyBundleIpfsAddress);
                },
                "gm.setGlobalModelAndSignatureAndKeyBundle.waitForCidUpdate",
                Math.max(rpcMaxAttempts + 4, 10),
            );
            if (!gmUpdated) {
                throw error;
            }
            receipt = pendingHash
                ? await waitForTransactionReceiptByHash(
                    pendingHash,
                    {
                        receiptLabel: "gm.setGlobalModelAndSignatureAndKeyBundle.resumeReceipt",
                        attempts: Math.max(rpcMaxAttempts + 12, 18),
                        pollBaseDelayMs: Math.max(rpcBaseDelayMs, 3000),
                        pollMaxDelayMs: Math.max(rpcMaxDelayMs, 30000),
                    },
                ).catch(() => null)
                : null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const setLastRoundAggregator = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setLastRoundAggregator().estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.setLastRoundAggregator.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.setLastRoundAggregator().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.setLastRoundAggregator.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
// set the contribution of the devices
export const setContribution = async (deviceID) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const method = contract.methods.incrementContribution(deviceID);
    try {
        const targetAddress = deviceID?.[0] || account.address;
        const previousScore = Number(await contract.methods.getContribution(targetAddress).call().catch(() => 0));
        const gasPrice = await getGasPriceWithRetry("gm.incrementContribution.getGasPrice");
        const gasEstimate = await estimateGasWithRetry(method, { from: account.address }, "gm.incrementContribution.estimateGas");
        const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.incrementContribution.send.getTransactionCount");
        const tx = {
            from: account.address,
            to: address,
            gas: BigInt(gasEstimate).toString(),
            gasPrice,
            nonce,
            data: method.encodeABI(),
        };
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        let receipt;
        try {
            receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.incrementContribution.send", signedTx.transactionHash);
        } catch (error) {
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error)) {
                throw error;
            }
            console.warn("Contribution increment transaction may already be pending onchain; waiting for updated score");
            const contributionUpdated = await waitForSuccessCondition(
                async () => {
                    const latestScore = Number(await contract.methods.getContribution(targetAddress).call().catch(() => previousScore));
                    return latestScore > previousScore;
                },
                "gm.incrementContribution.waitForScoreUpdate",
                Math.max(rpcMaxAttempts + 4, 10),
            );
            if (!contributionUpdated) {
                throw error;
            }
            receipt = null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        await logWorkerScore(targetAddress, "increment");
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
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
    } catch (error) {
        console.error("Error reading worker score:", serializeError(error));
    }
};

export const penalizeContribution = async (deviceIDs, reason) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.penalizeContribution(deviceIDs, reason).estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.penalizeContribution.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.penalizeContribution(deviceIDs, reason).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.penalizeContribution.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        for (const deviceID of deviceIDs || []) {
            await logWorkerScore(deviceID, reason || "penalty");
        }
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const submitModel = async (modelHash) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const method = contract.methods.submitModel(modelHash);
    try {
        const currentRound = Number(await getRound());
        const gasPrice = await getGasPriceWithRetry("gm.submitModel.getGasPrice");
        const gasEstimate = await estimateGasWithRetry(method, { from: account.address }, "gm.submitModel.estimateGas");
        const nonce = await getTransactionCountWithRetry(account.address, "pending", "gm.submitModel.send.getTransactionCount");
        const tx = {
            from: account.address,
            to: address,
            gas: BigInt(gasEstimate).toString(),
            gasPrice,
            nonce,
            data: method.encodeABI(),
        };
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        let receipt;
        try {
            receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "gm.submitModel.send", signedTx.transactionHash);
        } catch (error) {
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error)) {
                throw error;
            }
            console.warn("Model submission transaction may already be pending onchain; waiting for submitted-model flag");
            const submissionRecorded = await waitForSuccessCondition(
                async () => Boolean(await hasSubmittedModel(currentRound, account.address)),
                "gm.submitModel.waitForSubmission",
                Math.max(rpcMaxAttempts + 4, 10),
            );
            if (!submissionRecorded) {
                throw error;
            }
            receipt = null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const hasSubmittedModel = async (round, address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, gm_storage_address);
    const result = await callMethod(contract.methods.hasSubmittedModel(round, address), {}, "gm.hasSubmittedModel");
    return result;
};

export const getTopContributor = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    return await readContractValue(contract.methods.getTopContributor(), "gm.getTopContributor");
};
export const getContribution = async (address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, gm_storage_address);
    return await readContractValue(contract.methods.getContribution(address), "gm.getContribution");
};
export const getRound = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    return await readContractValue(contract.methods.getRound(), "gm.getRound");
};
export const incrementRound = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const method = contract.methods.incrementRound();
    try {
        const previousRound = Number(await getRound());
        let receipt;
        let gasPrice;
        try {
            const txResult = await signAndSendContractTx({
                account,
                to: address,
                method,
                gasPriceLabel: "gm.incrementRound.getGasPrice",
                gasEstimateLabel: "gm.incrementRound.estimateGas",
                sendLabel: "gm.incrementRound.send",
                gasBufferPercent: 30n,
            });
            receipt = txResult.receipt;
            gasPrice = txResult.gasPrice;
        } catch (error) {
            gasPrice = await getGasPriceWithRetry("gm.incrementRound.getGasPrice.fallback");
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error) && !isTransactionBlockTimeoutError(error)) {
                throw error;
            }
            const pendingHash = error?.receipt?.transactionHash
                || error?.transactionHash
                || error?.cause?.receipt?.transactionHash
                || error?.cause?.transactionHash;
            console.warn("Round increment transaction may already be pending onchain; waiting for round advance");
            const roundAdvanced = await waitForSuccessCondition(
                async () => Number(await getRound()) > previousRound,
                "gm.incrementRound.waitForRoundAdvance",
                Math.max(rpcMaxAttempts + 12, 18),
            );
            if (!roundAdvanced) {
                throw error;
            }
            receipt = pendingHash
                ? await waitForTransactionReceiptByHash(
                    pendingHash,
                    {
                        receiptLabel: "gm.incrementRound.resumeReceipt",
                        attempts: Math.max(rpcMaxAttempts + 12, 18),
                        pollBaseDelayMs: Math.max(rpcBaseDelayMs, 3000),
                        pollMaxDelayMs: Math.max(rpcMaxDelayMs, 30000),
                    },
                ).catch(() => null)
                : null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
// get current state from aggregator
export const getCurrentState = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    return await readContractValue(contract.methods.getSystemState(), "aggregator.getSystemState");
};
export const setCurrentState = async (newState) => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const method = contract.methods.setSystemState(newState);
    let gasPrice;
    try {
        const currentState = await readContractValue(contract.methods.getSystemState(), "aggregator.setSystemState.preflightState");
        console.log(`[aggregator.setSystemState] ${account.address} switching state from ${currentState?.[0] || currentState} to ${newState}`);
        gasPrice = await getGasPriceWithRetry("aggregator.setSystemState.getGasPrice");
        const gasEstimate = await estimateGasWithRetry(method, { from: account.address }, "aggregator.setSystemState.estimateGas");
        const nonce = await getTransactionCountWithRetry(account.address, "pending", "aggregator.setSystemState.send.getTransactionCount");
        const tx = {
            from: account.address,
            to: address,
            gas: BigInt(gasEstimate).toString(),
            gasPrice,
            nonce,
            data: method.encodeABI(),
        };
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        let receipt;
        try {
            receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "aggregator.setSystemState.send", signedTx.transactionHash);
        } catch (error) {
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error)) {
                throw error;
            }
            console.warn("setSystemState transaction may already be pending onchain; waiting for target state");
            receipt = null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        const stateChanged = await waitForSuccessCondition(
            async () => {
                const latestState = await readContractValue(contract.methods.getSystemState(), "aggregator.setSystemState.waitForState");
                return String(latestState?.[0] || latestState) === String(newState);
            },
            "aggregator.setSystemState.waitForState",
            Math.max(rpcMaxAttempts + 4, 10),
        );
        if (!stateChanged) {
            throw new Error(`Transaction confirmed but on-chain state did not become ${newState}`);
        }
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
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
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setBrokerEndpoint(newEndpoint).estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "aggregator.setBrokerEndpoint.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.setBrokerEndpoint(newEndpoint).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "aggregator.setBrokerEndpoint.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
export const triggerAggregatorSelection = async () => {
    const address = aggregator_address;
    const contract = getAggregatorSelectionContract();
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    try {
        logJson("[aggregator-selection] preflight diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
    } catch (error) {
        console.warn("[aggregator-selection] could not load preflight diagnostics:", serializeError(error));
    }
    const method = contract.methods.triggerAggregatorSelection();
    try {
        await callMethod(method, { from: account.address }, "aggregator.triggerAggregatorSelection.call");
        console.log("[aggregator-selection] eth_call simulation succeeded");
    }
    catch (error) {
        console.error("[aggregator-selection] eth_call simulation failed:", serializeError(error));
        throw error;
    }
    const gasPrice = await getGasPriceWithRetry("aggregator.triggerAggregatorSelection.getGasPrice");
    let gasEstimate;
    try {
        gasEstimate = await estimateGasWithRetry(method, { from: account.address }, "aggregator.triggerAggregatorSelection.estimateGas");
        console.log("[aggregator-selection] gas estimate:", gasEstimate.toString(), "gasPrice:", gasPrice.toString());
    }
    catch (error) {
        console.error("[aggregator-selection] gas estimate failed:", serializeError(error));
        throw error;
    }
    const gasLimit = withGasBuffer(gasEstimate);
    console.log("[aggregator-selection] gas limit with buffer:", gasLimit.toString());
    try {
        const previousState = await getCurrentState().catch(() => null);
        const previousRound = Number(await getRound().catch(() => 0));
        const previousAggregator = previousState?.[1] || null;
        const nonce = await getTransactionCountWithRetry(account.address, "pending", "aggregator.triggerAggregatorSelection.getTransactionCount");
        const tx = {
            from: account.address,
            to: address,
            gas: gasLimit.toString(),
            gasPrice: gasPrice,
            nonce,
            data: method.encodeABI(),
        };
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        let receipt;
        try {
            receipt = await sendSignedTransactionWithRetry(
                signedTx.rawTransaction,
                "aggregator.triggerAggregatorSelection.send",
                signedTx.transactionHash,
            );
        } catch (error) {
            if (!isTransactionNotFoundError(error) && !isReplacementUnderpricedError(error)) {
                throw error;
            }
            console.warn("Aggregator selection transaction may already be pending onchain; waiting for TRAINING state");
            const selectionApplied = await waitForSuccessCondition(
                async () => {
                    const latestState = await getCurrentState();
                    const latestRound = Number(await getRound().catch(() => previousRound));
                    const latestSystemState = String(latestState?.[0] || "");
                    const latestAggregator = latestState?.[1] || null;
                    return latestSystemState === "TRAINING"
                        && latestRound >= previousRound
                        && (latestAggregator !== previousAggregator || latestSystemState !== String(previousState?.[0] || ""));
                },
                "aggregator.triggerAggregatorSelection.waitForTrainingState",
                Math.max(rpcMaxAttempts + 4, 10),
            );
            if (!selectionApplied) {
                throw error;
            }
            receipt = null;
        }
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        try {
            logJson("[aggregator-selection] post-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
        } catch (error) {
            console.warn("[aggregator-selection] could not load post-transaction diagnostics:", serializeError(error));
        }
        return receipt;
    }
    catch (error) {
        console.error("[aggregator-selection] transaction failed:", serializeError(error));
        try {
            logJson("[aggregator-selection] failed-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
        } catch (diagnosticsError) {
            console.warn("[aggregator-selection] could not load failed-transaction diagnostics:", serializeError(diagnosticsError));
        }
        console.error("Error sending transaction: ", error);
        throw error;
    }
    
};

export const reportAggregatorTimeout = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.reportAggregatorTimeout().estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "aggregator.reportAggregatorTimeout.getTransactionCount");
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.reportAggregatorTimeout().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "aggregator.reportAggregatorTimeout.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
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
    const result = await callMethod(contract.methods.isAuthorized(address), {}, "registry.isAuthorized");
    return result;
};

export const getAuthorizedDevices = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await callMethod(contract.methods.getAuthorizedDevices(), {}, "registry.getAuthorizedDevices");
    return Array.from(result || []);
};

// get device public key (bytes) from registry by device address
export const getDevicePublicKey = async (address) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await callMethod(contract.methods.getDevice(address), {}, "registry.getDevice");
    // web3 may return both array indices and named fields
    const publicKey = (result && (result.public_key ?? result[3])) ?? "0x";
    return publicKey;
};

const hasUsableOnchainDevicePublicKey = async (address) => {
    const publicKey = String(await getDevicePublicKey(address).catch(() => "") || "").trim();
    return Boolean(publicKey && publicKey !== "0x");
};

const waitForAuthorizedRegistration = async (address, label) => {
    const authorized = await waitForSuccessCondition(
        async () => await isAuthorized(address) && await hasUsableOnchainDevicePublicKey(address),
        label,
        Math.max(rpcMaxAttempts + 4, 10),
    );
    return authorized;
};

export const registerDeviceWithTeeQuote = async (quoteHex, address, publicIp, brokerIp, publicKeyBytesHex) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    if (await isAuthorized(address) && await hasUsableOnchainDevicePublicKey(address)) {
        console.log("Device is already authorized onchain. Skipping duplicate registration.");
        return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
    }
    const method = contract.methods.registerDevice(quoteHex, address, publicIp, brokerIp, publicKeyBytesHex);
    const sendRegistrationTx = async () => {
        return await signAndSendContractTx({
            account,
            to: device_registry_address,
            method,
            gasPriceLabel: "registry.registerDevice.getGasPrice",
            gasEstimateLabel: "registry.registerDevice.estimateGas",
            sendLabel: "registry.registerDevice.send",
            gasBufferPercent: 30n,
        });
    };
    try {
        const { receipt, gasPrice } = await sendRegistrationTx();
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        if (isReplacementUnderpricedError(error) || isTransactionNotFoundError(error)) {
            console.warn("Registration transaction may already be pending onchain; waiting for authorization state");
            const authorized = await waitForAuthorizedRegistration(address, "registry.registerDevice.waitForAuthorization");
            if (authorized) {
                console.log("Device registration completed via an existing pending transaction.");
                return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
            }

            console.warn("Device is still not authorized; retrying registration with a freshly fetched pending nonce");
            try {
                const { receipt, gasPrice } = await sendRegistrationTx();
                logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
                console.log("Transaction receipt: ", receipt);
                return receipt;
            } catch (retryError) {
                if (!isReplacementUnderpricedError(retryError) && !isTransactionNotFoundError(retryError)) {
                    throw retryError;
                }
                console.warn("Registration retry is still pending/inconsistent on the RPC; waiting once more for authorization state");
                const authorizedAfterRetry = await waitForAuthorizedRegistration(address, "registry.registerDevice.waitForAuthorization.afterRetry");
                if (authorizedAfterRetry) {
                    console.log("Device registration completed after retry via an existing pending transaction.");
                    return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
                }
                throw retryError;
            }
        }
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const registerDeviceWithTeeQuoteAndRtmr3Events = async (
    quoteHex,
    rtmr3EventDigests,
    address,
    publicIp,
    brokerIp,
    publicKeyBytesHex
) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    if (await isAuthorized(address) && await hasUsableOnchainDevicePublicKey(address)) {
        console.log("Device is already authorized onchain. Skipping duplicate RTMR3 registration.");
        return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
    }
    const method = contract.methods.registerDeviceWithRtmr3Events(
        quoteHex,
        rtmr3EventDigests,
        address,
        publicIp,
        brokerIp,
        publicKeyBytesHex,
    );
    const sendRegistrationTx = async () => {
        return await signAndSendContractTx({
            account,
            to: device_registry_address,
            method,
            gasPriceLabel: "registry.registerDeviceWithRtmr3Events.getGasPrice",
            gasEstimateLabel: "registry.registerDeviceWithRtmr3Events.estimateGas",
            sendLabel: "registry.registerDeviceWithRtmr3Events.send",
            gasBufferPercent: 30n,
        });
    };
    try {
        const { receipt, gasPrice } = await sendRegistrationTx();
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.warn("[registry.registerDeviceWithRtmr3Events] send error diagnostics:", serializeError(error));
        if (isReplacementUnderpricedError(error) || isTransactionNotFoundError(error)) {
            console.warn("TDX RTMR3 registration transaction may already be pending onchain; waiting for authorization state");
            const authorized = await waitForAuthorizedRegistration(address, "registry.registerDeviceWithRtmr3Events.waitForAuthorization");
            if (authorized) {
                console.log("Device RTMR3 registration completed via an existing pending transaction.");
                return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
            }

            console.warn("Device is still not authorized; retrying RTMR3 registration with a freshly fetched pending nonce");
            try {
                const { receipt, gasPrice } = await sendRegistrationTx();
                logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
                console.log("Transaction receipt: ", receipt);
                return receipt;
            } catch (retryError) {
                if (!isReplacementUnderpricedError(retryError) && !isTransactionNotFoundError(retryError)) {
                    throw retryError;
                }
                console.warn("TDX RTMR3 registration retry is still pending/inconsistent on the RPC; waiting once more for authorization state");
                const authorizedAfterRetry = await waitForAuthorizedRegistration(address, "registry.registerDeviceWithRtmr3Events.waitForAuthorization.afterRetry");
                if (authorizedAfterRetry) {
                    console.log("Device RTMR3 registration completed after retry via an existing pending transaction.");
                    return { status: 1n, transactionHash: null, blockNumber: null, gasUsed: 0n };
                }
                throw retryError;
            }
        }
        console.error("Error sending TDX RTMR3 registration transaction: ", error);
        throw error;
    }
};

export const leaveDeviceRegistry = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.leaveNetwork().estimateGas({ from: account.address });
    const nonce = await getTransactionCountWithRetry(account.address, "pending", "registry.leaveNetwork.getTransactionCount");
    const tx = {
        from: account.address,
        to: device_registry_address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        nonce,
        data: contract.methods.leaveNetwork().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await sendSignedTransactionWithRetry(signedTx.rawTransaction, "registry.leaveNetwork.send", signedTx.transactionHash);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
