"""
Mend's view of the bounty escrow contract, and the one-time testnet setup.

Account layout created by `mend setup` (all sub-accounts of the owner):

  <owner>            the human. Funds the treasury and approves payouts.
  escrow.<owner>     the contract.
  operator.<owner>   the Mend server. May only open and reject incidents.
  recon.<owner>      recon agent. May only call submit_recon.
  forge-rules.<owner>, forge-llm.<owner>   forge agents. May only call submit_patch.
  guard.<owner>      guard agent. May only call attest.

Each agent's runtime key is a function-call access key scoped to its single
contract method, with a gas allowance. A leaked agent key cannot transfer the
agent's earnings or touch the treasury. The owner's key is added to each agent
account as the full-access custody key.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mend.integrations import near
from mend.integrations.near import KeyPair, NearAccount, NearError, NearRpc, TxResult, near_to_yocto

FAUCET_URL = "https://helper.testnet.near.org/account"
CONTRACT_WASM = Path(__file__).resolve().parent.parent / "contract" / "mend_bounty_escrow.wasm"

AGENT_METHODS: Dict[str, List[str]] = {
    "operator": ["open_incident", "reject"],
    "recon": ["submit_recon"],
    "forge-rules": ["submit_patch"],
    "forge-llm": ["submit_patch"],
    "guard": ["attest"],
}
FORGE_ROLES = {"rules": "forge-rules", "llm": "forge-llm"}

CONTRACT_FUNDING = "3"
AGENT_FUNDING = "0.3"
AGENT_GAS_ALLOWANCE = "0.25"
TREASURY_DEPOSIT = "2"
MAX_BOUNTY = "0.5"


@dataclass
class Deployment:
    network: str
    contract_id: str
    owner_id: str
    accounts: Dict[str, str]  # role -> account id
    repos: List[str]

    def public_dict(self) -> Dict[str, Any]:
        return {
            "network": self.network,
            "contract_id": self.contract_id,
            "owner_id": self.owner_id,
            "accounts": self.accounts,
            "repos": self.repos,
        }


class EscrowClient:
    """Signs each contract call with the account that is supposed to make it."""

    def __init__(self, keys: Dict[str, Any], rpc: Optional[NearRpc] = None) -> None:
        self.network = keys["network"]
        self.rpc = rpc or NearRpc(self.network)
        self.contract_id: str = keys["contract_id"]
        self.repos: List[str] = list(keys.get("repos", []))
        self._accounts: Dict[str, NearAccount] = {
            role: NearAccount(entry["account_id"], KeyPair.from_string(entry["secret_key"]), self.rpc)
            for role, entry in keys["accounts"].items()
        }

    @classmethod
    def load(cls, keys_file: Path) -> Optional["EscrowClient"]:
        if not keys_file.exists():
            return None
        return cls(json.loads(keys_file.read_text(encoding="utf-8")))

    def account_id(self, role: str) -> str:
        return self._accounts[role].account_id

    def forge_account_id(self, strategy: str) -> str:
        return self.account_id(FORGE_ROLES[strategy])

    def deployment(self) -> Deployment:
        return Deployment(
            network=self.network,
            contract_id=self.contract_id,
            owner_id=self.account_id("owner"),
            accounts={role: acct.account_id for role, acct in self._accounts.items()},
            repos=self.repos,
        )

    def _call(self, role: str, method: str, args: Dict[str, Any], final: bool = False) -> TxResult:
        return self._accounts[role].call(
            self.contract_id, method, args, wait_until="FINAL" if final else "EXECUTED_OPTIMISTIC"
        )

    # -- writes ------------------------------------------------------------

    def open_incident(self, incident_id: str, repo_id: str, crash_hash: str, bounty_near: str) -> TxResult:
        return self._call(
            "operator", "open_incident",
            {"incident_id": incident_id, "repo_id": repo_id, "crash_hash": crash_hash,
             "bounty": str(near_to_yocto(bounty_near))},
        )

    def submit_recon(self, incident_id: str, report_hash: str) -> TxResult:
        return self._call("recon", "submit_recon", {"incident_id": incident_id, "report_hash": report_hash})

    def submit_patch(self, strategy: str, incident_id: str, patch_hash: str, tests_run: int, tests_failed: int) -> TxResult:
        return self._call(
            FORGE_ROLES[strategy], "submit_patch",
            {"incident_id": incident_id, "patch_hash": patch_hash, "tests_run": tests_run, "tests_failed": tests_failed},
        )

    def attest(self, incident_id: str, strategy: str, patch_hash: str, passed: bool, score: int, report_hash: str) -> TxResult:
        return self._call(
            "guard", "attest",
            {"incident_id": incident_id, "forge": self.forge_account_id(strategy), "patch_hash": patch_hash,
             "passed": passed, "score": score, "report_hash": report_hash},
        )

    def approve(self, incident_id: str, strategy: str, patch_hash: str) -> TxResult:
        """Signed by the owner. Waits for finality because it moves funds."""
        return self._call(
            "owner", "approve",
            {"incident_id": incident_id, "forge": self.forge_account_id(strategy), "patch_hash": patch_hash},
            final=True,
        )

    def top_up(self, repo_id: str, amount_near: str) -> TxResult:
        """The owner account adds funds to a repository treasury (anyone may call `deposit`)."""
        return self._accounts["owner"].call(self.contract_id, "deposit", {"repo_id": repo_id},
                                            deposit=near_to_yocto(amount_near), wait_until="FINAL")

    def send_near(self, account_id: str, amount_near: str) -> TxResult:
        """Transfers testnet NEAR from Mend's funding account (used for the one-time wallet drip)."""
        return self._accounts["owner"].send(account_id, [near.transfer(near_to_yocto(amount_near))], wait_until="FINAL")

    def has_funds_for(self, repo_id: str, bounty_near: str) -> bool:
        repo = self.get_repo(repo_id) or {}
        return int(repo.get("balance", 0)) >= near_to_yocto(bounty_near)

    def reject(self, incident_id: str) -> TxResult:
        return self._call("operator", "reject", {"incident_id": incident_id})

    # -- views -------------------------------------------------------------

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        return self.rpc.view(self.contract_id, "get_incident", {"incident_id": incident_id})

    def get_repo(self, repo_id: str) -> Optional[Dict[str, Any]]:
        return self.rpc.view(self.contract_id, "get_repo", {"repo_id": repo_id})

    def get_agent(self, account_id: str) -> Dict[str, Any]:
        return self.rpc.view(self.contract_id, "get_agent", {"account_id": account_id})

    def get_stats(self) -> Dict[str, Any]:
        return self.rpc.view(self.contract_id, "get_stats", {})

    def overview(self) -> Dict[str, Any]:
        """Live treasury, agent earnings and balances, read from the chain."""
        agents = []
        for role, acct in self._accounts.items():
            if role in ("owner", "operator"):
                continue
            stats = self.get_agent(acct.account_id)
            agents.append({
                "role": role,
                "account_id": acct.account_id,
                "balance": str(acct.balance()),
                "explorer_url": self.rpc.account_url(acct.account_id),
                **stats,
            })
        return {
            "network": self.network,
            "contract_id": self.contract_id,
            "contract_url": self.rpc.account_url(self.contract_id),
            "owner_id": self.account_id("owner"),
            "stats": self.get_stats(),
            "repos": {repo: self.get_repo(repo) for repo in self.repos},
            "agents": agents,
        }


# ---------------------------------------------------------------------------
# One-time testnet setup
# ---------------------------------------------------------------------------

def _faucet_create(account_id: str, public_key: str) -> None:
    body = json.dumps({"newAccountId": account_id, "newAccountPublicKey": public_key}).encode()
    req = urllib.request.Request(FAUCET_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=near.SSL_CTX) as resp:
            resp.read()
    except Exception as exc:
        raise NearError(f"Testnet faucet could not create {account_id}: {exc}") from exc


def _save(keys_file: Path, state: Dict[str, Any]) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = keys_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(keys_file)


def setup_testnet(
    keys_file: Path,
    repos: List[str],
    log: Callable[[str], None] = print,
    owner_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates the accounts, deploys the contract, and registers each repository.
    Progress is saved after every step, so a failed run resumes where it stopped.
    """
    if not CONTRACT_WASM.exists():
        raise NearError(f"Contract build not found at {CONTRACT_WASM}. Run contracts/build.sh first.")
    rpc = NearRpc("testnet")
    state: Dict[str, Any] = (
        json.loads(keys_file.read_text(encoding="utf-8")) if keys_file.exists()
        else {"network": "testnet", "accounts": {}, "repos": [], "setup_txs": {}}
    )
    state.setdefault("setup_txs", {})

    # 1. Owner account, funded by the testnet faucet.
    if "owner" not in state["accounts"]:
        key = KeyPair.generate()
        owner_id = f"{owner_name or 'mend-' + secrets.token_hex(4)}.testnet"
        log(f"Creating owner account {owner_id} from the testnet faucet")
        _faucet_create(owner_id, key.public_key)
        state["accounts"]["owner"] = {"account_id": owner_id, "secret_key": key.secret_key}
        _save(keys_file, state)
    owner_entry = state["accounts"]["owner"]
    owner_key = KeyPair.from_string(owner_entry["secret_key"])
    owner = NearAccount(owner_entry["account_id"], owner_key, rpc)
    if rpc.view_account(owner.account_id) is None:
        raise NearError(f"Owner account {owner.account_id} does not exist on testnet")

    # 2. Contract account: created, funded, deployed and initialised in one transaction.
    contract_id = state.get("contract_id") or f"escrow.{owner.account_id}"
    if rpc.view_account(contract_id) is None:
        log(f"Deploying the escrow contract to {contract_id}")
        result = owner.send(
            contract_id,
            [
                near.create_account(),
                near.transfer(near_to_yocto(CONTRACT_FUNDING)),
                near.add_full_access_key(owner_key.public_key),
                near.deploy_contract(CONTRACT_WASM.read_bytes()),
                near.function_call("new", {}),
            ],
            wait_until="FINAL",
        )
        state["setup_txs"]["deploy"] = result.explorer_url
    state["contract_id"] = contract_id
    _save(keys_file, state)

    # 3. Agent accounts, each with a function-call key limited to its own method.
    for role, methods in AGENT_METHODS.items():
        if role in state["accounts"]:
            continue
        account_id = f"{role}.{owner.account_id}"
        key = KeyPair.generate()
        if rpc.view_account(account_id) is None:
            log(f"Creating {account_id} (may only call: {', '.join(methods)})")
            result = owner.send(
                account_id,
                [
                    near.create_account(),
                    near.transfer(near_to_yocto(AGENT_FUNDING)),
                    near.add_full_access_key(owner_key.public_key),
                    near.add_function_call_key(
                        key.public_key, contract_id, methods, allowance=near_to_yocto(AGENT_GAS_ALLOWANCE)
                    ),
                ],
                wait_until="FINAL",
            )
            state["setup_txs"][f"create_{role}"] = result.explorer_url
        else:
            # Account exists from an interrupted run whose key was not saved: add a fresh scoped key.
            log(f"Adding a new scoped key to existing account {account_id}")
            NearAccount(account_id, owner_key, rpc).send(
                account_id,
                [near.add_function_call_key(key.public_key, contract_id, methods,
                                            allowance=near_to_yocto(AGENT_GAS_ALLOWANCE))],
                wait_until="FINAL",
            )
        state["accounts"][role] = {"account_id": account_id, "secret_key": key.secret_key}
        _save(keys_file, state)

    # 4. Register each repository treasury and its agent allowlist.
    for repo in repos:
        register_repo_treasury(keys_file, repo, log=log)
    state = json.loads(keys_file.read_text(encoding="utf-8"))

    log(f"Setup complete. Contract: {rpc.account_url(contract_id)}")
    return state


def register_repo_treasury(keys_file: Path, repo: str, log: Callable[[str], None] = print,
                           deposit_near: str = TREASURY_DEPOSIT, max_bounty_near: str = MAX_BOUNTY) -> Dict[str, Any]:
    """
    Opens a treasury for `repo` on the deployed contract, funded by the owner,
    and allowlists this deployment's agents for it. Safe to call again.
    """
    state: Dict[str, Any] = json.loads(keys_file.read_text(encoding="utf-8"))
    rpc = NearRpc(state["network"])
    contract_id = state["contract_id"]
    owner_entry = state["accounts"]["owner"]
    owner = NearAccount(owner_entry["account_id"], KeyPair.from_string(owner_entry["secret_key"]), rpc)
    accounts = {role: entry["account_id"] for role, entry in state["accounts"].items()}
    txs = state.setdefault("setup_txs", {})

    if rpc.view(contract_id, "get_repo", {"repo_id": repo}) is None:
        log(f"Registering treasury for {repo} with {deposit_near} NEAR")
        result = owner.call(
            contract_id, "register_repo",
            {"repo_id": repo, "operator": accounts["operator"], "max_bounty": str(near_to_yocto(max_bounty_near))},
            deposit=near_to_yocto(deposit_near), wait_until="FINAL",
        )
        txs[f"register:{repo}"] = result.explorer_url
    registered = rpc.view(contract_id, "get_repo", {"repo_id": repo}) or {}
    if not registered.get("guard_agent"):
        log(f"Setting the agent allowlist for {repo}")
        result = owner.call(
            contract_id, "set_agents",
            {"repo_id": repo, "recon_agent": accounts["recon"], "guard_agent": accounts["guard"],
             "forge_agents": [accounts["forge-rules"], accounts["forge-llm"]]},
            wait_until="FINAL",
        )
        txs[f"agents:{repo}"] = result.explorer_url
    if repo not in state["repos"]:
        state["repos"].append(repo)
    _save(keys_file, state)
    return rpc.view(contract_id, "get_repo", {"repo_id": repo}) or {}
