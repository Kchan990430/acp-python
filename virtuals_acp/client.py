# virtuals_acp/client.py

import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from importlib.metadata import version
from typing import List, Optional, Tuple, Union, Dict, Any, Callable

import requests
import socketio
import socketio.client
from web3 import Web3

from virtuals_acp.base_contract_manager import BaseACPContractManager
from virtuals_acp.exceptions import ACPApiError, ACPError
from virtuals_acp.job import ACPJob
from virtuals_acp.memo import ACPMemo
from virtuals_acp.models import (
    ACPAgentSort,
    ACPJobPhase,
    ACPGraduationStatus,
    ACPOnlineStatus,
    MemoType,
    IACPAgent,
    IDeliverable,
    FeeType,
    GenericPayload,
    T,
    ACPMemoStatus,
)
from virtuals_acp.offering import ACPJobOffering


class VirtualsACP:
    def __init__(
        self,
        contract_manager: BaseACPContractManager,
        on_new_task: Optional[Callable] = None,
        on_evaluate: Optional[Callable] = None,
    ):
        self.contract_manager = contract_manager
        self.acp_api_url = contract_manager.config.acp_api_url

        # Socket.IO setup
        self.on_new_task = on_new_task
        self.on_evaluate = on_evaluate or self._default_on_evaluate
        self.sio = socketio.Client()
        self._setup_socket_handlers()
        self._connect_socket()

    def _default_on_evaluate(self, _: ACPJob) -> Tuple[bool, str]:
        """Default handler for job evaluation events."""
        return True, "Succesful"

    def _on_room_joined(self, data):
        print("Connected to room", data)  # Send acknowledgment back to server
        return True

    def _on_evaluate(self, data):
        print("--------------------------------")
        print(f"Evaluating job {data}")
        print("--------------------------------")
        if self.on_evaluate:
            print(f"Evaluating job {data}")
            try:
                threading.Thread(target=self.handle_evaluate, args=(data,)).start()
                return True
            except Exception as e:
                print(f"Error in onEvaluate handler: {e}")
                return False

    def _on_new_task(self, data):
        if self.on_new_task:
            try:
                threading.Thread(target=self.handle_new_task, args=(data,)).start()
                return True
            except Exception as e:
                print(f"Error in onNewTask handler: {e}")
                return False

    def handle_new_task(self, data) -> None:
        memo_to_sign_id = data.get("memoToSign")

        memos = [
            ACPMemo(
                id=memo.get("id"),
                type=MemoType(int(memo.get("memoType"))),
                content=memo.get("content"),
                next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                status=ACPMemoStatus(memo.get("status")),
                signed_reason=memo.get("signedReason"),
                expiry=(
                    datetime.fromtimestamp(int(memo["expiry"]))
                    if memo.get("expiry")
                    else None
                ),
            )
            for memo in data["memos"]
        ]

        memo_to_sign = (
            next((m for m in memos if int(m.id) == int(memo_to_sign_id)), None)
            if memo_to_sign_id is not None
            else None
        )

        context = data["context"]
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except json.JSONDecodeError:
                context = None

        job = ACPJob(
            acp_client=self,
            id=data["id"],
            provider_address=data["providerAddress"],
            client_address=data["clientAddress"],
            evaluator_address=data["evaluatorAddress"],
            memos=memos,
            phase=data["phase"],
            price=data["price"],
            context=context,
        )

        if self.on_new_task:
            self.on_new_task(job, memo_to_sign)

    def handle_evaluate(self, data) -> None:
        memos = [
            ACPMemo(
                id=memo.get("id"),
                type=MemoType(int(memo.get("memoType"))),
                content=memo.get("content"),
                next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                status=ACPMemoStatus(memo.get("status")),
                signed_reason=memo.get("signedReason"),
                expiry=(
                    datetime.fromtimestamp(int(memo["expiry"]))
                    if memo.get("expiry")
                    else None
                ),
            )
            for memo in data["memos"]
        ]

        context = data["context"]
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except json.JSONDecodeError:
                context = None

        job = ACPJob(
            acp_client=self,
            id=data["id"],
            provider_address=data["providerAddress"],
            client_address=data["clientAddress"],
            evaluator_address=data["evaluatorAddress"],
            memos=memos,
            phase=data["phase"],
            price=data["price"],
            context=context,
        )

        self.on_evaluate(job)

    def _setup_socket_handlers(self) -> None:
        self.sio.on("roomJoined", self._on_room_joined)
        self.sio.on("onEvaluate", self._on_evaluate)
        self.sio.on("onNewTask", self._on_new_task)

    def _connect_socket(self) -> None:
        """Connect to the socket server with appropriate authentication."""
        headers_data = {
            "x-sdk-version": version("virtuals_acp"),
            "x-sdk-language": "python",
        }
        auth_data = {"walletAddress": self.agent_address}

        if self.on_evaluate != self._default_on_evaluate:
            auth_data["evaluatorAddress"] = self.agent_address

        try:
            self.sio.connect(
                self.acp_api_url,
                auth=auth_data,
                headers=headers_data,
                transports=["websocket"],
            )

            def signal_handler(sig, frame):
                self.sio.disconnect()
                sys.exit(0)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

        except Exception as e:
            print(f"Failed to connect to socket server: {e}")

    def __del__(self):
        """Cleanup when the object is destroyed."""
        if hasattr(self, "sio") and self.sio is not None:
            self.sio.disconnect()

    @property
    def agent_address(self) -> str:
        return self.contract_manager.agent_wallet_address

    def browse_agents(
        self,
        keyword: str,
        cluster: Optional[str] = None,
        sort_by: Optional[List[ACPAgentSort]] = None,
        top_k: Optional[int] = None,
        graduation_status: Optional[ACPGraduationStatus] = None,
        online_status: Optional[ACPOnlineStatus] = None,
    ) -> List[IACPAgent]:
        url = f"{self.acp_api_url}/agents/v2/search?search={keyword}"
        top_k = 5 if top_k is None else top_k

        if sort_by:
            url += f"&sortBy={','.join([s.value for s in sort_by])}"

        if top_k:
            url += f"&top_k={top_k}"

        if self.agent_address:
            url += f"&walletAddressesToExclude={self.agent_address}"

        if cluster:
            url += f"&cluster={cluster}"

        if graduation_status is not None:
            url += f"&graduationStatus={graduation_status.value}"

        if online_status is not None:
            url += f"&onlineStatus={online_status.value}"

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()

            agents_data = data.get("data", [])
            agents = []
            for agent_data in agents_data:
                offerings = [
                    ACPJobOffering(
                        acp_client=self,
                        provider_address=agent_data["walletAddress"],
                        name=offering["name"],
                        price=offering["price"],
                        price_usd=offering["priceUsd"],
                        requirement_schema=offering.get("requirementSchema", None),
                    )
                    for offering in agent_data.get("offerings", [])
                ]

                agents.append(
                    IACPAgent(
                        id=agent_data["id"],
                        name=agent_data.get("name"),
                        description=agent_data.get("description"),
                        wallet_address=Web3.to_checksum_address(
                            agent_data["walletAddress"]
                        ),
                        offerings=offerings,
                        twitter_handle=agent_data.get("twitterHandle"),
                        metrics=agent_data.get("metrics"),
                        processing_time=agent_data.get("processingTime", ""),
                    )
                )
            return agents
        except requests.exceptions.RequestException as e:
            raise ACPApiError(f"Failed to browse agents: {e}")
        except Exception as e:
            raise ACPError(f"An unexpected error occurred while browsing agents: {e}")

    def initiate_job(
        self,
        provider_address: str,
        service_requirement: Union[Dict[str, Any], str],
        amount: float,
        evaluator_address: Optional[str] = None,
        expired_at: Optional[datetime] = None,
    ) -> int:
        if expired_at is None:
            expired_at = datetime.now(timezone.utc) + timedelta(days=1)

        eval_addr = (
            Web3.to_checksum_address(evaluator_address)
            if evaluator_address
            else self.agent_address
        )

        if provider_address == self.agent_address:
            raise Exception("You cannot initiate a job with yourself as the provider")

        user_op_hash = self.contract_manager.create_job(
            provider_address, eval_addr, expired_at
        )
        job_id = self.contract_manager.get_job_id(user_op_hash)

        self.contract_manager.set_budget_with_payment_token(job_id, amount)

        self.contract_manager.create_memo(
            job_id,
            (
                service_requirement
                if isinstance(service_requirement, str)
                else json.dumps(service_requirement)
            ),
            MemoType.MESSAGE,
            is_secured=True,
            next_phase=ACPJobPhase.NEGOTIATION,
        )

        return job_id

    def respond_to_job(
        self,
        job_id: int,
        memo_id: int,
        accept: bool,
        content: Optional[str],
        reason: Optional[str] = "",
    ) -> str:
        tx_hash = self.contract_manager.sign_memo(memo_id, accept, reason or "")
        if not accept:
            return tx_hash

        return self.contract_manager.create_memo(
            job_id,
            content or f"Job {job_id} accepted.{f' {reason}' or ''}",
            MemoType.MESSAGE,
            is_secured=False,
            next_phase=ACPJobPhase.TRANSACTION,
        )

    def pay_job(
        self,
        job_id: int,
        memo_id: int,
        amount: float,
        reason: Optional[str] = "",
    ) -> str:

        self.contract_manager.approve_allowance(amount)

        self.contract_manager.sign_memo(memo_id, True, reason or "")

        reason = f"{reason if reason else f'Job {job_id} paid.'}"

        return self.contract_manager.create_memo(
            job_id,
            reason,
            MemoType.MESSAGE,
            is_secured=False,
            next_phase=ACPJobPhase.EVALUATION,
        )

    def request_funds(
        self,
        job_id: int,
        amount: float,
        receiver_address: str,
        fee_amount: float,
        fee_type: FeeType,
        reason: GenericPayload[T],
        next_phase: ACPJobPhase,
        expired_at: datetime,
    ) -> str:
        receiver_address = Web3.to_checksum_address(receiver_address)

        return self.contract_manager.create_payable_memo(
            job_id,
            json.dumps(reason.model_dump()),
            amount,
            receiver_address,
            fee_amount,
            fee_type,
            next_phase,
            MemoType.PAYABLE_REQUEST,
            expired_at,
        )

    def respond_to_funds_request(
        self,
        memo_id: int,
        accept: bool,
        amount: float,
        reason: Optional[str] = "",
    ) -> str:
        if not accept:
            self.contract_manager.sign_memo(memo_id, False, reason)

        if amount > 0:
            self.contract_manager.approve_allowance(amount)

        return self.contract_manager.sign_memo(memo_id, True, reason)

    def transfer_funds(
        self,
        job_id: int,
        amount: float,
        receiver_address: str,
        fee_amount: float,
        fee_type: FeeType,
        reason: GenericPayload[T],
        next_phase: ACPJobPhase,
        expired_at: datetime,
    ) -> str:
        total_amount = amount + fee_amount

        if total_amount > 0:
            self.contract_manager.approve_allowance(total_amount)

        tx_hash = self.contract_manager.create_payable_memo(
            job_id,
            json.dumps(reason.model_dump()),
            amount,
            receiver_address,
            fee_amount,
            fee_type,
            next_phase,
            MemoType.PAYABLE_TRANSFER_ESCROW,
            expired_at,
        )

        return tx_hash

    def send_message(
        self, job_id: int, message: GenericPayload[T], next_phase: ACPJobPhase
    ) -> str:
        return self.contract_manager.create_memo(
            job_id,
            json.dumps(message.model_dump()),
            MemoType.MESSAGE,
            False,
            next_phase,
        )

    def respond_to_funds_transfer(
        self, memo_id: int, accept: bool, reason: Optional[str] = ""
    ):
        return self.contract_manager.sign_memo(memo_id, accept, reason)

    def deliver_job(self, job_id: int, deliverable: IDeliverable) -> str:
        return self.contract_manager.create_memo(
            job_id,
            deliverable.model_dump_json(),
            MemoType.OBJECT_URL,
            is_secured=True,
            next_phase=ACPJobPhase.COMPLETED,
        )

    def sign_memo(self, memo_id: int, accept: bool, reason: Optional[str] = "") -> str:
        return self.contract_manager.sign_memo(memo_id, accept, reason)

    def get_active_jobs(self, page: int = 1, pageSize: int = 10) -> List["ACPJob"]:
        url = f"{self.acp_api_url}/jobs/active?pagination[page]={page}&pagination[pageSize]={pageSize}"
        headers = {"wallet-address": self.agent_address}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            jobs = []

            for job in data.get("data", []):
                memos = []
                for memo in job.get("memos", []):
                    memos.append(
                        ACPMemo(
                            id=memo.get("id"),
                            type=MemoType(int(memo.get("memoType"))),
                            content=memo.get("content"),
                            next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                            status=ACPMemoStatus(memo.get("status")),
                            signed_reason=memo.get("signedReason"),
                            expiry=(
                                datetime.fromtimestamp(int(memo["expiry"]))
                                if memo.get("expiry")
                                else None
                            ),
                        )
                    )

                context = job.get("context")
                if isinstance(context, str):
                    try:
                        context = json.loads(context)
                    except json.JSONDecodeError:
                        context = None

                jobs.append(
                    ACPJob(
                        acp_client=self,
                        id=job.get("id"),
                        provider_address=job.get("providerAddress"),
                        client_address=job.get("clientAddress"),
                        evaluator_address=job.get("evaluatorAddress"),
                        memos=memos,
                        phase=job.get("phase"),
                        price=job.get("price"),
                        context=context,
                    )
                )
            return jobs
        except Exception as e:
            raise ACPApiError(f"Failed to get active jobs: {e}")

    def get_completed_jobs(self, page: int = 1, pageSize: int = 10) -> List["ACPJob"]:
        url = f"{self.acp_api_url}/jobs/completed?pagination[page]={page}&pagination[pageSize]={pageSize}"
        headers = {"wallet-address": self.agent_address}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            jobs = []

            for job in data.get("data", []):
                memos = []
                for memo in job.get("memos", []):
                    memos.append(
                        ACPMemo(
                            id=memo.get("id"),
                            type=MemoType(int(memo.get("memoType"))),
                            content=memo.get("content"),
                            next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                            status=ACPMemoStatus(memo.get("status")),
                            signed_reason=memo.get("signedReason"),
                            expiry=(
                                datetime.fromtimestamp(int(memo["expiry"]))
                                if memo.get("expiry")
                                else None
                            ),
                        )
                    )

                context = job.get("context")
                if isinstance(context, str):
                    try:
                        context = json.loads(context)
                    except json.JSONDecodeError:
                        context = None

                jobs.append(
                    ACPJob(
                        acp_client=self,
                        id=job.get("id"),
                        provider_address=job.get("providerAddress"),
                        client_address=job.get("clientAddress"),
                        evaluator_address=job.get("evaluatorAddress"),
                        memos=memos,
                        phase=job.get("phase"),
                        price=job.get("price"),
                        context=context,
                    )
                )
            return jobs
        except Exception as e:
            raise ACPApiError(f"Failed to get completed jobs: {e}")

    def get_cancelled_jobs(self, page: int = 1, pageSize: int = 10) -> List["ACPJob"]:
        url = f"{self.acp_api_url}/jobs/cancelled?pagination[page]={page}&pagination[pageSize]={pageSize}"
        headers = {"wallet-address": self.agent_address}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            jobs = []

            for job in data.get("data", []):
                memos = []
                for memo in job.get("memos", []):
                    memos.append(
                        ACPMemo(
                            id=memo.get("id"),
                            type=MemoType(int(memo.get("memoType"))),
                            content=memo.get("content"),
                            next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                            status=ACPMemoStatus(memo.get("status")),
                            signed_reason=memo.get("signedReason"),
                            expiry=(
                                datetime.fromtimestamp(int(memo["expiry"]))
                                if memo.get("expiry")
                                else None
                            ),
                        )
                    )

                context = job.get("context")
                if isinstance(context, str):
                    try:
                        context = json.loads(context)
                    except json.JSONDecodeError:
                        context = None

                jobs.append(
                    ACPJob(
                        acp_client=self,
                        id=job.get("id"),
                        provider_address=job.get("providerAddress"),
                        client_address=job.get("clientAddress"),
                        evaluator_address=job.get("evaluatorAddress"),
                        memos=memos,
                        phase=job.get("phase"),
                        price=job.get("price"),
                        context=context,
                    )
                )
            return jobs
        except Exception as e:
            raise ACPApiError(f"Failed to get cancelled jobs: {e}")

    def get_job_by_onchain_id(self, onchain_job_id: int) -> "ACPJob":
        url = f"{self.acp_api_url}/jobs/{onchain_job_id}"
        headers = {"wallet-address": self.agent_address}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if data.get("error"):
                raise ACPApiError(data["error"]["message"])

            memos = []
            for memo in data.get("data", {}).get("memos", []):
                memos.append(
                    ACPMemo(
                        id=memo.get("id"),
                        type=MemoType(int(memo.get("memoType"))),
                        content=memo.get("content"),
                        next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                        status=ACPMemoStatus(memo.get("status")),
                        signed_reason=memo.get("signedReason"),
                        expiry=(
                            datetime.fromtimestamp(int(memo["expiry"]))
                            if memo.get("expiry")
                            else None
                        ),
                    )
                )

            context = data.get("data", {}).get("context")
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except json.JSONDecodeError:
                    context = None

            return ACPJob(
                acp_client=self,
                id=data.get("data", {}).get("id"),
                provider_address=data.get("data", {}).get("providerAddress"),
                client_address=data.get("data", {}).get("clientAddress"),
                evaluator_address=data.get("data", {}).get("evaluatorAddress"),
                memos=memos,
                phase=data.get("data", {}).get("phase"),
                price=data.get("data", {}).get("price"),
                context=context,
            )
        except Exception as e:
            raise ACPApiError(f"Failed to get job by onchain ID: {e}")

    def get_memo_by_id(self, onchain_job_id: int, memo_id: int) -> "ACPMemo":
        url = f"{self.acp_api_url}/jobs/{onchain_job_id}/memos/{memo_id}"
        headers = {"wallet-address": self.agent_address}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if data.get("error"):
                raise ACPApiError(data["error"]["message"])

            memo = data.get("data", {})

            return ACPMemo(
                id=memo.get("id"),
                type=MemoType(int(memo.get("memoType"))),
                content=memo.get("content"),
                next_phase=ACPJobPhase(int(memo.get("nextPhase"))),
                status=ACPMemoStatus(memo.get("status")),
                signed_reason=memo.get("signedReason"),
                expiry=(
                    datetime.fromtimestamp(int(memo["expiry"]))
                    if memo.get("expiry")
                    else None
                ),
            )

        except Exception as e:
            raise ACPApiError(f"Failed to get memo by ID: {e}")

    def get_agent(self, wallet_address: str) -> Optional[IACPAgent]:
        url = f"{self.acp_api_url}/agents?filters[walletAddress]={wallet_address}"

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()

            agents_data = data.get("data", [])
            if not agents_data:
                return None

            agent_data = agents_data[0]

            offerings = [
                ACPJobOffering(
                    acp_client=self,
                    provider_address=agent_data["walletAddress"],
                    name=offering["name"],
                    price=offering["price"],
                    price_usd=offering["priceUsd"],
                    requirement_schema=offering.get("requirementSchema", None),
                )
                for offering in agent_data.get("offerings", [])
            ]

            return IACPAgent(
                id=agent_data["id"],
                name=agent_data.get("name"),
                description=agent_data.get("description"),
                wallet_address=Web3.to_checksum_address(agent_data["walletAddress"]),
                offerings=offerings,
                twitter_handle=agent_data.get("twitterHandle"),
                metrics=agent_data.get("metrics"),
                processing_time=agent_data.get("processingTime", ""),
            )

        except requests.exceptions.RequestException as e:
            raise ACPApiError(f"Failed to get agent: {e}")
        except Exception as e:
            raise ACPError(f"An unexpected error occurred while getting agent: {e}")


# Rebuild the AcpJob model after VirtualsACP is defined
ACPJob.model_rebuild()
ACPMemo.model_rebuild()
ACPJobOffering.model_rebuild()
