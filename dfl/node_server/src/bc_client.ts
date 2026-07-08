// @ts-nocheck
import Web3 from "web3";
import fs from "fs";
import 'dotenv/config';
// setup client´
//const web3 = new Web3("https://eth-sepolia.g.alchemy.com/v2/pFowzUSGYob62Q7i2YVsF0LFUX3WiCT2");
const web3 = new Web3(process.env.SEPOLIA_RPC_URL);
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


// function to set global model
export const setGlobalModel = async (newIpfsAddress) => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setGlobalModel(newIpfsAddress).estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setGlobalModel(newIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setGlobalModelSignature(newSigIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setGlobalModelAndSignature(newModelIpfsAddress, newSigIpfsAddress).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods
        .setGlobalModelAndSignatureAndKeyBundle(
            newModelIpfsAddress,
            newSigIpfsAddress,
            newKeyBundleIpfsAddress,
        )
        .estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods
            .setGlobalModelAndSignatureAndKeyBundle(
                newModelIpfsAddress,
                newSigIpfsAddress,
                newKeyBundleIpfsAddress,
            )
            .encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setLastRoundAggregator().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.incrementContribution(deviceID).estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.incrementContribution(deviceID).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        await logWorkerScore(deviceID?.[0] || account.address, "increment");
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.penalizeContribution(deviceIDs, reason).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.submitModel(modelHash).estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.submitModel(modelHash).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const result = await contract.methods.hasSubmittedModel(round, address).call();
    return result;
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
export const incrementRound = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/gm.json", "utf-8"));
    const address = gm_storage_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.incrementRound().estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.incrementRound().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    let state = await contract.methods.getSystemState().call().then((result) => {
        return result;
    });
    return state;
};
export const setCurrentState = async (newState) => {
    const abi = JSON.parse(fs.readFileSync("./abi/AggregatorSelection.json", "utf-8"));
    const address = aggregator_address;
    const contract = new web3.eth.Contract(abi, address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.setSystemState(newState).estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setSystemState(newState).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.setBrokerEndpoint(newEndpoint).encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    logJson("[aggregator-selection] preflight diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
    const method = contract.methods.triggerAggregatorSelection();
    try {
        await method.call({ from: account.address });
        console.log("[aggregator-selection] eth_call simulation succeeded");
    }
    catch (error) {
        console.error("[aggregator-selection] eth_call simulation failed:", serializeError(error));
        throw error;
    }
    const gasPrice = await web3.eth.getGasPrice();
    let gasEstimate;
    try {
        gasEstimate = await method.estimateGas({ from: account.address });
        console.log("[aggregator-selection] gas estimate:", gasEstimate.toString(), "gasPrice:", gasPrice.toString());
    }
    catch (error) {
        console.error("[aggregator-selection] gas estimate failed:", serializeError(error));
        throw error;
    }
    const gasLimit = withGasBuffer(gasEstimate);
    console.log("[aggregator-selection] gas limit with buffer:", gasLimit.toString());
    const tx = {
        from: account.address,
        to: address,
        gas: gasLimit.toString(),
        gasPrice: gasPrice,
        data: method.encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        logJson("[aggregator-selection] post-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
        return receipt;
    }
    catch (error) {
        console.error("[aggregator-selection] transaction failed:", serializeError(error));
        logJson("[aggregator-selection] failed-transaction diagnostics", await getAggregatorSelectionDiagnostics(contract, account.address));
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
    const tx = {
        from: account.address,
        to: address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.reportAggregatorTimeout().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
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
    const result = await contract.methods.isAuthorized(address).call();
    return result;
};

export const getAuthorizedDevices = async () => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const result = await contract.methods.getAuthorizedDevices().call();
    return Array.from(result || []);
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

export const registerDeviceWithTeeQuote = async (quoteHex, address, publicIp, brokerIp, publicKeyBytesHex) => {
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods
        .registerDevice(quoteHex, address, publicIp, brokerIp, publicKeyBytesHex)
        .estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: device_registry_address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods
            .registerDevice(quoteHex, address, publicIp, brokerIp, publicKeyBytesHex)
            .encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};

export const registerDeviceWithTeeQuoteAndRtmr3Events = async (
    quoteHex,
    rtmr3EventDigests,
    composeHash,
    workerImageDigest,
    address,
    publicIp,
    brokerIp,
    publicKeyBytesHex
) => {
    if (!device_registry_address) {
        throw new Error("REGISTRY_ADDRESS is required for TDX device registration");
    }
    const abi = JSON.parse(fs.readFileSync("./abi/registry.json", "utf-8"));
    const contract = new web3.eth.Contract(abi, device_registry_address);
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const registration = contract.methods.registerDeviceWithRtmr3EventsAndImageDigest(
        quoteHex,
        rtmr3EventDigests,
        composeHash,
        workerImageDigest,
        address,
        publicIp,
        brokerIp,
        publicKeyBytesHex
    );
    const calldata = registration.encodeABI();
    const hexByteLength = (value) => {
        if (typeof value !== "string" || !/^0x[0-9a-fA-F]*$/.test(value) || value.length % 2 !== 0) {
            throw new Error("Invalid hex value in TDX registration payload");
        }
        return (value.length - 2) / 2;
    };
    console.log("TDX registration payload:", {
        quoteBytes: hexByteLength(quoteHex),
        eventDigestBytes: rtmr3EventDigests.map(hexByteLength),
        composeHashBytes: hexByteLength(composeHash),
        workerImageDigestBytes: hexByteLength(workerImageDigest),
        publicKeyBytes: hexByteLength(publicKeyBytesHex),
        calldataBytes: hexByteLength(calldata),
    });

    try {
        const attestationAddress = await contract.methods.tdxV4Attestation().call();
        const debugAbi = [{
            type: "function",
            name: "debugVerifyWithRtmr3Events",
            inputs: [
                { name: "input", type: "bytes" },
                { name: "rtmr3EventDigests", type: "bytes[]" },
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
        const debugResult = await attestation.methods
            .debugVerifyWithRtmr3Events(quoteHex, rtmr3EventDigests, composeHash)
            .call({ from: account.address });
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
    } catch (error) {
        console.error("TDX debug verification call failed:", serializeError(error));
    }

    const gasEstimate = await registration.estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: device_registry_address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: calldata,
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
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
    const account = web3.eth.accounts.privateKeyToAccount(privateKey);
    addAccountToWallet(account);
    const gasPrice = await web3.eth.getGasPrice();
    const gasEstimate = await contract.methods.leaveNetwork().estimateGas({ from: account.address });
    const tx = {
        from: account.address,
        to: device_registry_address,
        gas: gasEstimate,
        gasPrice: gasPrice,
        data: contract.methods.leaveNetwork().encodeABI(),
    };
    try {
        const signedTx = await web3.eth.accounts.signTransaction(tx, privateKey);
        const receipt = await web3.eth.sendSignedTransaction(signedTx.rawTransaction);
        logTransactionCost("worker", "contract_transaction", receipt, gasPrice);
        console.log("Transaction receipt: ", receipt);
        return receipt;
    }
    catch (error) {
        console.error("Error sending transaction: ", error);
        throw error;
    }
};
