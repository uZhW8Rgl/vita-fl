import Web3 from "web3";
const web3 = new Web3();
const EIP712_DOMAIN_TYPEHASH = web3.utils.keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
const MODEL_SUBMISSION_TYPEHASH = web3.utils.keccak256("ModelSubmission(uint256 round,address aggregator,address worker,bytes32 parentModelHash,bytes32 modelHash,bytes32 packageHash,uint256 nonce)");
const GM_STORAGE_NAME_HASH = web3.utils.keccak256("VITA-FL GMStorage");
const GM_STORAGE_VERSION_HASH = web3.utils.keccak256("1");
const uintString = (value, label) => {
    let parsed;
    try {
        parsed = BigInt(value);
    }
    catch {
        throw new Error(`${label} must be an unsigned integer.`);
    }
    if (parsed < 0n)
        throw new Error(`${label} must be an unsigned integer.`);
    return parsed.toString();
};
const address = (value, label) => {
    if (!/^0x[0-9a-fA-F]{40}$/.test(String(value || ""))) {
        throw new Error(`${label} must be a 20-byte Ethereum address.`);
    }
    return value;
};
const bytes32 = (value, label) => {
    if (!/^0x[0-9a-fA-F]{64}$/.test(String(value || ""))) {
        throw new Error(`${label} must be exactly 32 bytes.`);
    }
    return value;
};
const hashEncoded = (types, values) => web3.utils.keccak256(web3.eth.abi.encodeParameters(types, values));
export const deriveModelSubmissionDigest = ({ chainId, verifyingContract, round, aggregator, worker, parentModelHash, modelHash, packageHash, nonce, }) => {
    const domainSeparator = hashEncoded(["bytes32", "bytes32", "bytes32", "uint256", "address"], [
        EIP712_DOMAIN_TYPEHASH,
        GM_STORAGE_NAME_HASH,
        GM_STORAGE_VERSION_HASH,
        uintString(chainId, "chainId"),
        address(verifyingContract, "verifyingContract"),
    ]);
    const structHash = hashEncoded([
        "bytes32",
        "uint256",
        "address",
        "address",
        "bytes32",
        "bytes32",
        "bytes32",
        "uint256",
    ], [
        MODEL_SUBMISSION_TYPEHASH,
        uintString(round, "round"),
        address(aggregator, "aggregator"),
        address(worker, "worker"),
        bytes32(parentModelHash, "parentModelHash"),
        bytes32(modelHash, "modelHash"),
        bytes32(packageHash, "packageHash"),
        uintString(nonce, "nonce"),
    ]);
    return web3.utils.keccak256(`0x1901${domainSeparator.slice(2)}${structHash.slice(2)}`);
};
