# virtuals_acp/contract_manager.py

import math
import time
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any

from eth_account import Account
from web3 import Web3
from web3.contract import Contract

from virtuals_acp.abi import ACP_ABI, ERC20_ABI
from virtuals_acp.alchemy import AlchemyAccountKit
from virtuals_acp.base_contract_manager import BaseACPContractManager
from virtuals_acp.configs import ACPContractConfig, DEFAULT_CONFIG
from virtuals_acp.models import ACPJobPhase, MemoType, FeeType


class ACPContractManager(BaseACPContractManager):
    def __init__(
        self,
        wallet_private_key: str,
        entity_id: int,
        agent_wallet_address: str,
        config: ACPContractConfig = DEFAULT_CONFIG,
    ):
        super().__init__(
            agent_wallet_address=agent_wallet_address,
            config=config,
        )

        self.account = Account.from_key(wallet_private_key.removeprefix("0x"))
        self.alchemy_kit = AlchemyAccountKit(
            agent_wallet_address, entity_id, self.account, config.chain_id
        )

        self.contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.contract_address), abi=ACP_ABI
        )
        self.token_contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.payment_token_address),
            abi=ERC20_ABI,
        )

    def _format_amount(self, amount: float) -> int:
        amount_decimal = Decimal(str(amount))
        return int(amount_decimal * (10**self.config.payment_token_decimals))

    def _sign_transaction(
        self, method_name: str, args: list, contract_address: Optional[str] = None
    ) -> str:
        if contract_address:
            encoded_data = self.token_contract.encode_abi(method_name, args=args)
        else:
            encoded_data = self.contract.encode_abi(method_name, args=args)

        trx_data = [
            {
                "to": (
                    contract_address
                    if contract_address
                    else self.config.contract_address
                ),
                "data": encoded_data,
            }
        ]

        self.alchemy_kit.create_session()
        send_result = self.alchemy_kit.execute_calls(trx_data)
        user_op_hash = self.alchemy_kit.get_user_operation_hash(send_result)

        return user_op_hash

    def validate_transaction(self, hash_value: str) -> Dict[str, Any]:
        retries = 3
        while retries > 0:
            try:
                result = self.alchemy_kit.get_calls_status(hash_value)

                if result.get("status") == 200:
                    return result.get("receipts", [])[0].get("transactionHash")
                else:
                    raise Exception(f"Failed to validate transaction")
            except Exception as e:
                retries -= 1
                if retries == 0:
                    print(f"Error during validate_transaction: {e}")
                    raise
                time.sleep(2 * (3 - retries))

        raise Exception("Failed to validate transaction")

    def create_job(
        self, provider_address: str, evaluator_address: str, expired_at: datetime
    ) -> str:
        try:
            provider_address = Web3.to_checksum_address(provider_address)
            evaluator_address = Web3.to_checksum_address(evaluator_address)
            expire_timestamp = int(expired_at.timestamp())

            # Sign the transaction
            user_op_hash = self._sign_transaction(
                "createJob", [provider_address, evaluator_address, expire_timestamp]
            )
            return user_op_hash
        except Exception as e:
            raise Exception(f"Failed to create job {e}")

    def get_job_id(self, hash_value: str) -> int:
        retries = 3
        while retries > 0:
            try:
                result = self.alchemy_kit.get_calls_status(hash_value)

                if result.get("status") == 200:
                    logs = result.get("receipts", [])[0].get("logs", [])
                    contract_logs = next(
                        (
                            log
                            for log in logs
                            if log.get("address", "").lower()
                            == self.config.contract_address.lower()
                        ),
                        None,
                    )

                    if not contract_logs:
                        raise Exception("Failed to get contract logs")

                    try:
                        return int(Web3.to_int(hexstr=contract_logs.get("data")))
                    except (ValueError, TypeError, AttributeError):
                        raise Exception("Failed to parse job ID from contract logs")
                else:
                    raise Exception(f"Failed to get job id")
            except Exception as e:
                retries -= 1
                if retries == 0:
                    print(f"Error during get_job_id: {e}")
                    raise
                time.sleep(2 * (3 - retries))

        raise Exception("Failed to get job id")

    def approve_allowance(self, amount: float) -> Dict[str, Any]:
        user_op_hash = self._sign_transaction(
            "approve",
            [self.config.contract_address, self._format_amount(amount)],
            self.config.payment_token_address,
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - approve_allowance")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to approve allowance {e}")

    def create_payable_memo(
        self,
        job_id: int,
        content: str,
        amount: float,
        receiver_address: str,
        fee_amount: float,
        fee_type: FeeType,
        next_phase: ACPJobPhase,
        memo_type: MemoType,
        expired_at: datetime,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        receiver_address = Web3.to_checksum_address(receiver_address)
        token = self.config.payment_token_address if token is None else token

        user_op_hash = self._sign_transaction(
            "createPayableMemo",
            [
                job_id,
                content,
                token,
                self._format_amount(amount),
                receiver_address,
                self._format_amount(fee_amount),
                fee_type.value,
                memo_type.value,
                next_phase.value,
                math.floor(expired_at.timestamp()),
            ],
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - create_payable_memo")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to create payable memo {e}")

    def create_memo(
        self,
        job_id: int,
        content: str,
        memo_type: MemoType,
        is_secured: bool,
        next_phase: ACPJobPhase,
    ) -> Dict[str, Any]:
        user_op_hash = self._sign_transaction(
            "createMemo",
            [job_id, content, memo_type.value, is_secured, next_phase.value],
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - create_memo")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to create memo {e}")

    def sign_memo(
        self, memo_id: int, is_approved: bool, reason: Optional[str] = ""
    ) -> Dict[str, Any]:
        user_op_hash = self._sign_transaction(
            "signMemo", [memo_id, is_approved, reason]
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - sign_memo")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to sign memo {e}")

    def set_budget(self, job_id: int, budget: float) -> Dict[str, Any]:
        user_op_hash = self._sign_transaction(
            "setBudget", [job_id, self._format_amount(budget)]
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - set_budget")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to set budget {e}")

    def set_budget_with_payment_token(
        self,
        job_id: int,
        budget: float,
        payment_token_address: Optional[str] = None,
    ) -> Dict[str, Any]:

        if payment_token_address is None:
            payment_token_address = self.config.payment_token_address

        user_op_hash = self._sign_transaction(
            "setBudgetWithPaymentToken",
            [job_id, self._format_amount(budget), payment_token_address],
        )

        if user_op_hash is None:
            raise Exception("Failed to sign transaction - set_budget")

        try:
            return self.validate_transaction(user_op_hash)
        except Exception as e:
            raise Exception(f"Failed to set budget with payment token {e}")
