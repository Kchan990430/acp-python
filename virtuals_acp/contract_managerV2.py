import base64
import json
import math
from hexbytes import HexBytes
import requests
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend
from web3 import Web3
from web3.contract import Contract

from virtuals_acp.abi import ACP_ABI, ERC20_ABI
from virtuals_acp.base_contract_manager import BaseACPContractManager
from virtuals_acp.configs import ACPContractConfig, DEFAULT_CONFIG
from virtuals_acp.models import ACPJobPhase, MemoType, FeeType


class ACPContractManagerV2(BaseACPContractManager):
    def __init__(
        self,
        private_key_base64: str,
        agent_wallet_address: str,
        config: ACPContractConfig = DEFAULT_CONFIG,
    ):
        super().__init__(
            agent_wallet_address=agent_wallet_address,
            config=config,
        )
        self.config = config
        self.relay_url = config.acp_api_url + "/privy/proxy/relay-transaction"
        self.wallet_id = self._get_wallet_id()

        if not private_key_base64:
            raise ValueError("private_key_base64 is REQUIRED for V2 contract manager")

        self.private_key_base64 = private_key_base64

        self.contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.contract_address), abi=ACP_ABI
        )
        self.token_contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.payment_token_address),
            abi=ERC20_ABI,
        )

    def _get_wallet_id(self) -> str:
        agent_response = requests.get(
            self.config.acp_api_url
            + "/agents?filters[walletAddress]="
            + self.agent_wallet_address,
            headers={
                "Content-Type": "application/json",
            },
        )

        if agent_response.status_code != 200:
            raise Exception(
                f"Failed to get wallet_id: {agent_response.status_code} {agent_response.text}"
            )

        json_response = agent_response.json()

        data = json_response["data"]

        if data is None:
            raise Exception(f"No agent found for wallet {self.agent_wallet_address}")

        if len(data) == 0:
            raise Exception(f"No agent found for wallet {self.agent_wallet_address}")

        agent = data[0]

        if agent["walletId"] is None:
            raise Exception(f"No wallet_id found for agent {self.agent_wallet_address}")

        return agent["walletId"]

    def _format_amount(self, amount: float) -> int:
        amount_decimal = Decimal(str(amount))
        return int(amount_decimal * (10**self.config.payment_token_decimals))

    def _prepare_transaction(
        self, method_name: str, args: list, contract_address: Optional[str] = None
    ) -> Dict[str, Any]:
        if contract_address:
            encoded_data = self.token_contract.encode_abi(method_name, args=args)
        else:
            encoded_data = self.contract.encode_abi(method_name, args=args)

        return {
            "to": (
                contract_address if contract_address else self.config.contract_address
            ),
            "data": encoded_data,
            "value": "0x0",
        }

    def _generate_authorization_signature(
        self, transaction_data: Dict[str, Any], sponsor: bool = True
    ) -> str:
        body = {
            "method": "eth_sendTransaction",
            "caip2": f"eip155:{self.config.chain_id}",
            "chain_type": "ethereum",
            "sponsor": sponsor,
            "params": {"transaction": transaction_data},
        }
        if not self.config.privy_app_id:
            raise ValueError(
                "privy_app_id is required in config for V2 contract manager"
            )

        signature_headers = {"privy-app-id": self.config.privy_app_id}
        payload = {
            "version": 1,
            "method": "POST",
            "url": f"https://api.privy.io/v1/wallets/{self.wallet_id}/rpc",
            "body": body,
            "headers": signature_headers,
        }
        serialized_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        clean_key = self.private_key_base64.replace("wallet-auth:", "")
        private_key_pem = (
            f"-----BEGIN PRIVATE KEY-----\n{clean_key}\n-----END PRIVATE KEY-----"
        )

        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None, backend=default_backend()
            )

            signature = private_key.sign(  # type: ignore
                serialized_payload.encode(), ec.ECDSA(hashes.SHA256())  # type: ignore
            )

            signature_b64 = base64.b64encode(signature).decode()

            return signature_b64

        except Exception as e:
            raise Exception(f"Failed to generate authorization signature: {e}")

    def _send_transaction(
        self, transaction_data: Dict[str, Any], authorization_signature: str
    ) -> str:
        headers = {
            "Content-Type": "application/json",
            "privy-authorization-signature": authorization_signature,
        }

        body = {
            "walletId": self.wallet_id,
            "chainId": self.config.chain_id,
            "sponsor": True,
            "transactionData": {
                "to": transaction_data["to"],
                "data": transaction_data["data"],
                "value": transaction_data.get("value", "0x0"),
            },
        }

        response = requests.post(self.relay_url, headers=headers, json=body)

        response.raise_for_status()
        result = response.json()
        if "hash" in result and "success" in result:
            return result["hash"]
        else:
            raise Exception(f"Unexpected response format: {result}")

    def create_job(
        self, provider_address: str, evaluator_address: str, expired_at: datetime
    ) -> str:
        provider_address = Web3.to_checksum_address(provider_address)
        evaluator_address = Web3.to_checksum_address(evaluator_address)
        expire_timestamp = int(expired_at.timestamp())
        tx_data = self._prepare_transaction(
            "createJob", [provider_address, evaluator_address, expire_timestamp]
        )
        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

    def get_job_id(self, hash_value: str) -> int:
        hash32 = HexBytes(hash_value)
        receipt = self.w3.eth.wait_for_transaction_receipt(hash32)

        logs = receipt.get("logs", [])
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
            return int(Web3.to_int(contract_logs.get("data")))
        except (ValueError, TypeError, AttributeError):
            raise Exception("Failed to parse job ID from contract logs")

    def approve_allowance(self, amount: float) -> str:
        tx_data = self._prepare_transaction(
            "approve",
            [self.config.contract_address, self._format_amount(amount)],
            self.config.payment_token_address,
        )

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

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
    ) -> str:
        receiver_address = Web3.to_checksum_address(receiver_address)
        token = self.config.payment_token_address if token is None else token

        tx_data = self._prepare_transaction(
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

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

    def create_memo(
        self,
        job_id: int,
        content: str,
        memo_type: MemoType,
        is_secured: bool,
        next_phase: ACPJobPhase,
    ) -> str:
        tx_data = self._prepare_transaction(
            "createMemo",
            [job_id, content, memo_type.value, is_secured, next_phase.value],
        )

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

    def sign_memo(
        self, memo_id: int, is_approved: bool, reason: Optional[str] = ""
    ) -> str:
        tx_data = self._prepare_transaction("signMemo", [memo_id, is_approved, reason])

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

    def set_budget(self, job_id: int, budget: float) -> str:
        tx_data = self._prepare_transaction(
            "setBudget", [job_id, self._format_amount(budget)]
        )

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)

    def set_budget_with_payment_token(
        self, job_id: int, budget: float, payment_token_address: Optional[str] = None
    ) -> str:
        if payment_token_address is None:
            payment_token_address = self.config.payment_token_address

        tx_data = self._prepare_transaction(
            "setBudgetWithPaymentToken",
            [job_id, self._format_amount(budget), payment_token_address],
        )

        auth_signature = self._generate_authorization_signature(tx_data)
        return self._send_transaction(tx_data, auth_signature)
