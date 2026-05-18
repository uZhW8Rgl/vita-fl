import axios from "axios";
import fs from "fs";
import FormData from "form-data";
import { setGlobalModelAndSignature, getCurrentGM, getCurrentGMSignature } from "./bc_client.js";
export const pinFile = async (filePath) => {
    try {
        const formData = new FormData();
        const file = fs.createReadStream(filePath);
        formData.append("file", file);
        const pinataMetadata = JSON.stringify({
            name: "File name",
        });
        formData.append("pinataMetadata", pinataMetadata);
        const pinataOptions = JSON.stringify({
            cidVersion: 1,
        });
        formData.append("pinataOptions", pinataOptions);
        const res = await axios.post("https://api.pinata.cloud/pinning/pinFileToIPFS", formData, {
            headers: {
                Authorization: `Bearer ${process.env.PINATA_JWT}`,
            },
        });
        return res.data.IpfsHash;
    }
    catch (error) {
        console.log(error);
    }
};
export const getFileFromIPFS = async (hash, outPath = "./data/gm.bin") => {
    try {
        //const res = await axios.get(`https://turquoise-zestful-squirrel-651.mypinata.cloud/ipfs/${hash}`);
        //const res = await axios.get(process.env.IPFS_GATEWAY + `/ipfs/${hash}`);
        const res = await axios.get("https://plum-peaceful-flea-894.mypinata.cloud/ipfs/${hash}");
        // write the file which is in binary format to the local file system
        fs.writeFileSync(outPath, res.data, { encoding: "binary" });
        console.log("File written to the local file system");
    }
    catch (error) {
        console.log(error);
    }
};
export const updateGM = async () => {
    const modelPath = "./data/results_iid/aggregated.bin";
    const sigPath = "./data/results_iid/aggregated.bin.sig";
    const modelCid = await pinFile(modelPath);
    if (!modelCid)
        throw new Error("Pinning failed for model, no CID returned");
    const sigCid = await pinFile(sigPath);
    if (!sigCid)
        throw new Error("Pinning failed for signature, no CID returned");
    console.log("New GM CID:", modelCid);
    console.log("New GM SIG CID:", sigCid);
    await setGlobalModelAndSignature(modelCid, sigCid);
    console.log("Global model + signature updated (on-chain) ");
};
export const getCurrentModel = async () => {
    const modelCid = String(await getCurrentGM() || "");
    const sigCid = String(await getCurrentGMSignature() || "");
    console.log("Model CID:", modelCid);
    console.log("Sig CID:", sigCid);
    if (modelCid.length > 0) {
        await getFileFromIPFS(modelCid, "./data/gm.bin");
    }
    if (sigCid.length > 0) {
        await getFileFromIPFS(sigCid, "./data/gm.bin.sig");
    }
    console.log("Global model fetched" + (sigCid ? " + signature" : ""));
};
