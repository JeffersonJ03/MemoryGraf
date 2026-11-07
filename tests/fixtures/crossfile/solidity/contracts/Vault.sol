pragma solidity ^0.8.0;
import "./Math.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import * as U from "./Util.sol";
import "forge-std/Test.sol";
contract Vault is Ownable, Base {
    function deposit(uint v) public onlyOwner returns (uint) {
        uint r = Math.mul(v, 2);
        r = U.helper(r);
        baseFn();
        return freeFn(r);
    }
}
