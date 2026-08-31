// @ts-nocheck
import crypto from "crypto";
import { sign } from "web3-eth-accounts";
const telemetryUrl = String(process.env.DFL_TELEMETRY_URL || "").replace(/\/+$/, "");
const privateKey = String(process.env.PRIVATE_KEY || "");
const accountAddress = String(process.env.ACCOUNT_ADDRESS || "").toLowerCase();
const deviceId = String(process.env.DEVICE_ID || "");
const deliveryAttempts = 5;
const requestTimeoutMs = 3000;
function canonicalValue(value) {
    if (Array.isArray(value))
        return value.map(canonicalValue);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])]));
    }
    return value;
}
function endpointUrl() {
    if (telemetryUrl.endsWith("/api/telemetry/events"))
        return telemetryUrl;
    return `${telemetryUrl}/api/telemetry/events`;
}
export async function emitTelemetryEvent(event, attributes = {}) {
    if (!telemetryUrl || !privateKey || !/^0x[0-9a-f]{40}$/i.test(accountAddress))
        return;
    const payload = {
        account: accountAddress,
        attributes: canonicalValue(attributes),
        device_id: deviceId,
        event: String(event),
        nonce: crypto.randomUUID(),
        timestamp_unix_ms: Date.now(),
        version: 1,
    };
    const canonicalPayload = JSON.stringify(canonicalValue(payload));
    const signature = sign(canonicalPayload, privateKey).signature;
    const envelope = JSON.stringify({ payload, signature });
    for (let attempt = 1; attempt <= deliveryAttempts; attempt += 1) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), requestTimeoutMs);
        try {
            const response = await fetch(endpointUrl(), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: envelope,
                signal: controller.signal,
            });
            if (response.ok)
                return;
            const responseBody = await response.text();
            if (response.status === 400 && responseBody.includes("telemetry nonce was already used")) {
                // The first request was committed but its response was lost.
                return;
            }
            if (response.status < 500) {
                console.warn(`Telemetry event ${event} was rejected with HTTP ${response.status}.`);
                return;
            }
            if (attempt === deliveryAttempts) {
                console.warn(`Telemetry event ${event} failed after ${deliveryAttempts} attempts with HTTP ${response.status}.`);
            }
        }
        catch (error) {
            if (attempt === deliveryAttempts) {
                console.warn(`Telemetry event ${event} could not be delivered after ${deliveryAttempts} attempts:`, error?.message || String(error));
            }
        }
        finally {
            clearTimeout(timeout);
        }
        if (attempt < deliveryAttempts) {
            await new Promise((resolve) => setTimeout(resolve, Math.min(250 * (2 ** (attempt - 1)), 2000)));
        }
    }
}
