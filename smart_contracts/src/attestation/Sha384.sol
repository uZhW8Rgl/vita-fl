// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library Sha384 {
    uint256 internal constant RTMR_SIZE = 48;

    function hashRtmrExtend(bytes memory currentRtmr, bytes memory eventDigest) internal pure returns (bytes memory) {
        require(currentRtmr.length == RTMR_SIZE, "current RTMR must be 48 bytes");
        require(eventDigest.length == RTMR_SIZE, "event digest must be 48 bytes");
        return hash(abi.encodePacked(currentRtmr, eventDigest));
    }

    function hash(bytes memory input) internal pure returns (bytes memory) {
        uint256 paddedLength = ((input.length + 17 + 127) / 128) * 128;
        bytes memory paddedInput = new bytes(paddedLength);

        for (uint256 i = 0; i < input.length; i++) {
            paddedInput[i] = input[i];
        }
        paddedInput[input.length] = bytes1(0x80);

        uint256 bitLength = input.length * 8;
        for (uint256 i = 0; i < 8; i++) {
            paddedInput[paddedLength - 1 - i] = bytes1(uint8(bitLength >> (8 * i)));
        }

        uint64[8] memory state = [
            uint64(0xcbbb9d5dc1059ed8),
            0x629a292a367cd507,
            0x9159015a3070dd17,
            0x152fecd8f70e5939,
            0x67332667ffc00b31,
            0x8eb44a8768581511,
            0xdb0c2e0d64f98fa7,
            0x47b5481dbefa4fa4
        ];

        for (uint256 blockOffset = 0; blockOffset < paddedLength; blockOffset += 128) {
            uint64[80] memory w;
            for (uint256 i = 0; i < 16; i++) {
                w[i] = _readUint64(paddedInput, blockOffset + i * 8);
            }
            for (uint256 i = 16; i < 80; i++) {
                unchecked {
                    w[i] = _smallSigma1(w[i - 2]) + w[i - 7] + _smallSigma0(w[i - 15]) + w[i - 16];
                }
            }

            uint64[8] memory working;
            for (uint256 i = 0; i < 8; i++) working[i] = state[i];
            for (uint256 i = 0; i < 80; i++) {
                _round(working, w[i], _k(i));
            }

            unchecked {
                for (uint256 i = 0; i < 8; i++) state[i] += working[i];
            }
        }

        return abi.encodePacked(state[0], state[1], state[2], state[3], state[4], state[5]);
    }

    function _readUint64(bytes memory input, uint256 offset) private pure returns (uint64 value) {
        for (uint256 i = 0; i < 8; i++) {
            value = (value << 8) | uint64(uint8(input[offset + i]));
        }
    }

    function _rotr(uint64 x, uint64 n) private pure returns (uint64) {
        return (x >> n) | (x << (64 - n));
    }

    function _ch(uint64 x, uint64 y, uint64 z) private pure returns (uint64) {
        return (x & y) ^ (~x & z);
    }

    function _maj(uint64 x, uint64 y, uint64 z) private pure returns (uint64) {
        return (x & y) ^ (x & z) ^ (y & z);
    }

    function _bigSigma0(uint64 x) private pure returns (uint64) {
        return _rotr(x, 28) ^ _rotr(x, 34) ^ _rotr(x, 39);
    }

    function _bigSigma1(uint64 x) private pure returns (uint64) {
        return _rotr(x, 14) ^ _rotr(x, 18) ^ _rotr(x, 41);
    }

    function _smallSigma0(uint64 x) private pure returns (uint64) {
        return _rotr(x, 1) ^ _rotr(x, 8) ^ (x >> 7);
    }

    function _smallSigma1(uint64 x) private pure returns (uint64) {
        return _rotr(x, 19) ^ _rotr(x, 61) ^ (x >> 6);
    }

    function _round(uint64[8] memory state, uint64 word, uint64 k) private pure {
        unchecked {
            uint64 t1 = state[7] + _bigSigma1(state[4]) + _ch(state[4], state[5], state[6]) + k + word;
            uint64 t2 = _bigSigma0(state[0]) + _maj(state[0], state[1], state[2]);
            state[7] = state[6];
            state[6] = state[5];
            state[5] = state[4];
            state[4] = state[3] + t1;
            state[3] = state[2];
            state[2] = state[1];
            state[1] = state[0];
            state[0] = t1 + t2;
        }
    }

    function _k(uint256 i) private pure returns (uint64) {
        uint64[80] memory k = [
            uint64(0x428a2f98d728ae22),
            0x7137449123ef65cd,
            0xb5c0fbcfec4d3b2f,
            0xe9b5dba58189dbbc,
            0x3956c25bf348b538,
            0x59f111f1b605d019,
            0x923f82a4af194f9b,
            0xab1c5ed5da6d8118,
            0xd807aa98a3030242,
            0x12835b0145706fbe,
            0x243185be4ee4b28c,
            0x550c7dc3d5ffb4e2,
            0x72be5d74f27b896f,
            0x80deb1fe3b1696b1,
            0x9bdc06a725c71235,
            0xc19bf174cf692694,
            0xe49b69c19ef14ad2,
            0xefbe4786384f25e3,
            0x0fc19dc68b8cd5b5,
            0x240ca1cc77ac9c65,
            0x2de92c6f592b0275,
            0x4a7484aa6ea6e483,
            0x5cb0a9dcbd41fbd4,
            0x76f988da831153b5,
            0x983e5152ee66dfab,
            0xa831c66d2db43210,
            0xb00327c898fb213f,
            0xbf597fc7beef0ee4,
            0xc6e00bf33da88fc2,
            0xd5a79147930aa725,
            0x06ca6351e003826f,
            0x142929670a0e6e70,
            0x27b70a8546d22ffc,
            0x2e1b21385c26c926,
            0x4d2c6dfc5ac42aed,
            0x53380d139d95b3df,
            0x650a73548baf63de,
            0x766a0abb3c77b2a8,
            0x81c2c92e47edaee6,
            0x92722c851482353b,
            0xa2bfe8a14cf10364,
            0xa81a664bbc423001,
            0xc24b8b70d0f89791,
            0xc76c51a30654be30,
            0xd192e819d6ef5218,
            0xd69906245565a910,
            0xf40e35855771202a,
            0x106aa07032bbd1b8,
            0x19a4c116b8d2d0c8,
            0x1e376c085141ab53,
            0x2748774cdf8eeb99,
            0x34b0bcb5e19b48a8,
            0x391c0cb3c5c95a63,
            0x4ed8aa4ae3418acb,
            0x5b9cca4f7763e373,
            0x682e6ff3d6b2b8a3,
            0x748f82ee5defb2fc,
            0x78a5636f43172f60,
            0x84c87814a1f0ab72,
            0x8cc702081a6439ec,
            0x90befffa23631e28,
            0xa4506cebde82bde9,
            0xbef9a3f7b2c67915,
            0xc67178f2e372532b,
            0xca273eceea26619c,
            0xd186b8c721c0c207,
            0xeada7dd6cde0eb1e,
            0xf57d4f7fee6ed178,
            0x06f067aa72176fba,
            0x0a637dc5a2c898a6,
            0x113f9804bef90dae,
            0x1b710b35131c471b,
            0x28db77f523047d84,
            0x32caab7b40c72493,
            0x3c9ebe0a15c9bebc,
            0x431d67c49c100d4c,
            0x4cc5d4becb3e42b6,
            0x597f299cfc657e2a,
            0x5fcb6fab3ad6faec,
            0x6c44198c4a475817
        ];
        return k[i];
    }
}
