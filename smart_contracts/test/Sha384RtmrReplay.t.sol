// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Sha384} from "../src/attestation/Sha384.sol";

contract Sha384RtmrReplayTest is Test {
    function testPhalaComposeHashEventDigest() public pure {
        bytes32 composeHash = 0x3ffb565cd839ce58a10c635b84aa4d9bcf29488e0a97779df94a1102fc2f5b15;
        bytes memory digest = Sha384.hash(abi.encodePacked(bytes4(0x01000008), ":", "compose-hash", ":", composeHash));

        assertEq(
            digest,
            hex"a71a08c276737e800de0b4ad8ce3a948ab53043ef16b138e75bdeaf4358060a923593a2158c494d10c83d0d2024c91c0"
        );
    }

    function testReplayPhalaRtmr3EventLog() public pure {
        bytes[] memory digests = new bytes[](10);
        digests[0] =
            hex"f9974020ef507068183313d0ca808e0d1ca9b2d1ad0c61f5784e7157c362c06536f5ddacdad4451693f48fcc72fff624";
        digests[1] =
            hex"1b1e28e631176be7ebd94abcb25c70ed8e5d0048afc5bb6016f5ee55a8b9dde87856a6b75c2fc5911914388e2378f08c";
        digests[2] =
            hex"d57fe9bbd83b260e5ad6fd1f61f052214730c530b82132808c7a3c19ca57c6c2ce4592b791d94946f70dfe6733868048";
        digests[3] =
            hex"3a17929fd376a99b9a199ac88ede28937b22f44ab36a0b6f22eaea724dbf2d0b4d7518b63326a3a6118961f5a28046e1";
        digests[4] =
            hex"98bd7e6bd3952720b65027fd494834045d06b4a714bf737a06b874638b3ea00ff402f7f583e3e3b05e921c8570433ac6";
        digests[5] =
            hex"f90478ca858afb60d2b963a882e7508d2a88cbd348ed63a0de303bc59c82c2dfb14c3677e8ff00cc2ffeb84603e2f413";
        digests[6] =
            hex"ce4d96a1544f2f558070c691b7911370690ea3f7f6cd41e67dbd0a30a9f745ff72b1a4f6ccdc3cbfe6d0e794b72d66eb";
        digests[7] =
            hex"83368b43a0fc6f824f5a9220592df85fd30e2d405ecbd253a5c6354af63e6c9b41aec557c38a38e348ab87f9ac8fc68c";
        digests[8] =
            hex"ba51104636900268b0e059fa3d266419d079d1e94aea26fb9fcbb8d764bf4c89a67ac271b8a0d1a3989945132a111fc7";
        digests[9] =
            hex"1a76b2a80a0be71eae59f80945d876351a7a3fb8e9fd1ff1cede5734aa84ea11fd72b4edfbb6f04e5a85edd114c751bd";

        bytes memory rtmr = new bytes(48);
        for (uint256 i = 0; i < digests.length; i++) {
            rtmr = Sha384.hashRtmrExtend(rtmr, digests[i]);
        }

        assertEq(
            rtmr,
            hex"5c52273cd71e397fe9c7d824fb953b31f4c8dfc9eeed328db56f38df07c34a4eb0b8a595174b408afd19e72e106ceafa"
        );
    }
}
