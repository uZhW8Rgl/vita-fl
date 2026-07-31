export type DecimalRate = {
    text: string;
    coefficient: bigint;
    scale: number;
};

export const parseDecimalRate = (value: string, name: string): DecimalRate => {
    const text = String(value ?? "").trim();
    const match = /^([0-9]+)(?:\.([0-9]+))?$/.exec(text);
    if (!match) {
        throw new Error(`${name} must be a non-negative decimal without exponent notation.`);
    }
    const fraction = match[2] || "";
    return {
        text,
        coefficient: BigInt(`${match[1]}${fraction}`),
        scale: fraction.length,
    };
};

export const formatDecimalUnits = (value: bigint, decimals: number): string => {
    if (value < 0n) {
        throw new Error("formatDecimalUnits only accepts unsigned values.");
    }
    if (!Number.isSafeInteger(decimals) || decimals < 0) {
        throw new Error("decimals must be a non-negative safe integer.");
    }
    if (decimals === 0) return value.toString();

    const digits = value.toString().padStart(decimals + 1, "0");
    const integer = digits.slice(0, -decimals);
    const fraction = digits.slice(-decimals).replace(/0+$/, "");
    return fraction ? `${integer}.${fraction}` : integer;
};

export const valueWeiAtRate = (valueWei: bigint, rate: DecimalRate): string =>
    formatDecimalUnits(valueWei * rate.coefficient, 18 + rate.scale);

export const gweiRateToWei = (rate: DecimalRate): bigint => {
    if (rate.scale > 9) {
        throw new Error("A gas-price scenario may contain at most nine decimal places in Gwei.");
    }
    return rate.coefficient * (10n ** BigInt(9 - rate.scale));
};

export const transactionCostAmounts = (
    gasUsed: bigint,
    effectiveGasPriceWei: bigint,
    ethEurRate: DecimalRate,
    ethUsdRate?: DecimalRate,
) => {
    if (gasUsed < 0n || effectiveGasPriceWei < 0n) {
        throw new Error("Gas usage and effective gas price must be unsigned.");
    }
    const costWei = gasUsed * effectiveGasPriceWei;
    return {
        costWei: costWei.toString(),
        costGwei: formatDecimalUnits(costWei, 9),
        costEth: formatDecimalUnits(costWei, 18),
        costEur: valueWeiAtRate(costWei, ethEurRate),
        costUsd: ethUsdRate ? valueWeiAtRate(costWei, ethUsdRate) : "",
        effectiveGasPriceGwei: formatDecimalUnits(effectiveGasPriceWei, 9),
    };
};
