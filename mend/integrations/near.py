"""
NEAR client for Mend: key handling, transaction signing and JSON-RPC.

Every on-chain write in Mend goes through `NearAccount.send`, which builds a
Borsh-encoded transaction, signs it with the account's ed25519 key and submits
it with `send_tx`. Results come from the RPC response: the transaction hash,
the block it landed in, contract logs and any failure. Nothing here is derived
locally, so a transaction hash returned by this module can always be opened in
an explorer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from nacl.signing import SigningKey

YOCTO_PER_NEAR = 10**24
TGAS = 10**12
DEFAULT_GAS = 30 * TGAS

NETWORKS: Dict[str, Dict[str, Any]] = {
    "testnet": {
        "rpc": ["https://test.rpc.fastnear.com", "https://rpc.testnet.near.org"],
        "explorer": "https://testnet.nearblocks.io",
    },
    "mainnet": {
        "rpc": ["https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"],
        "explorer": "https://nearblocks.io",
    },
}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class NearError(Exception):
    """Raised when the RPC is unreachable or a transaction fails on-chain."""


def _ssl_context() -> ssl.SSLContext:
    # macOS system Python ships without a CA bundle wired up.
    cafile = "/etc/ssl/cert.pem"
    if Path(cafile).exists():
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


SSL_CTX = _ssl_context()


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _B58.find(ch)
        if idx < 0:
            raise ValueError("invalid base58 character")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def near_to_yocto(amount: str) -> int:
    """Converts a decimal NEAR string ("0.25") to yoctoNEAR without floats."""
    whole, _, frac = str(amount).partition(".")
    frac = (frac + "0" * 24)[:24]
    return int(whole or "0") * YOCTO_PER_NEAR + int(frac or "0")


def yocto_to_near(yocto: Any, places: int = 4) -> str:
    value = int(yocto)
    whole, rem = divmod(value, YOCTO_PER_NEAR)
    frac = str(rem).rjust(24, "0")[:places].rstrip("0")
    return f"{whole}.{frac}" if frac else str(whole)


class KeyPair:
    """An ed25519 key in NEAR's `ed25519:<base58>` string format."""

    def __init__(self, signing_key: SigningKey) -> None:
        self._sk = signing_key
        self.public_bytes = bytes(signing_key.verify_key)

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(SigningKey.generate())

    @classmethod
    def from_string(cls, secret: str) -> "KeyPair":
        raw = b58decode(secret.split(":", 1)[-1])
        if len(raw) not in (32, 64):
            raise ValueError("NEAR secret key must decode to 32 or 64 bytes")
        return cls(SigningKey(raw[:32]))

    @property
    def secret_key(self) -> str:
        return "ed25519:" + b58encode(bytes(self._sk) + self.public_bytes)

    @property
    def public_key(self) -> str:
        return "ed25519:" + b58encode(self.public_bytes)

    @property
    def implicit_account_id(self) -> str:
        return self.public_bytes.hex()

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message).signature


# ---------------------------------------------------------------------------
# Borsh encoding for transactions
# ---------------------------------------------------------------------------

def _u8(v: int) -> bytes:
    return struct.pack("<B", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _u128(v: int) -> bytes:
    return int(v).to_bytes(16, "little")


def _string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u32(len(raw)) + raw


def _public_key(public_bytes: bytes) -> bytes:
    return _u8(0) + public_bytes  # 0 = ed25519


def _public_key_from_string(public_key: str) -> bytes:
    return _public_key(b58decode(public_key.split(":", 1)[-1]))


def create_account() -> bytes:
    return _u8(0)


def deploy_contract(code: bytes) -> bytes:
    return _u8(1) + _u32(len(code)) + code


def function_call(method: str, args: Dict[str, Any], gas: int = DEFAULT_GAS, deposit: int = 0) -> bytes:
    raw = json.dumps(args, separators=(",", ":")).encode("utf-8")
    return _u8(2) + _string(method) + _u32(len(raw)) + raw + _u64(gas) + _u128(deposit)


def transfer(deposit: int) -> bytes:
    return _u8(3) + _u128(deposit)


def add_full_access_key(public_key: str) -> bytes:
    return _u8(5) + _public_key_from_string(public_key) + _u64(0) + _u8(1)


def add_function_call_key(
    public_key: str,
    receiver_id: str,
    method_names: Sequence[str],
    allowance: Optional[int] = None,
) -> bytes:
    """A key that can only call `method_names` on `receiver_id` and cannot move funds."""
    permission = _u8(0)
    permission += (_u8(1) + _u128(allowance)) if allowance is not None else _u8(0)
    permission += _string(receiver_id)
    permission += _u32(len(method_names)) + b"".join(_string(m) for m in method_names)
    return _u8(5) + _public_key_from_string(public_key) + _u64(0) + permission


@dataclass
class TxResult:
    tx_hash: str
    signer_id: str
    receiver_id: str
    block_hash: str
    explorer_url: str
    logs: List[str] = field(default_factory=list)
    return_value: Any = None
    gas_burnt: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_hash": self.tx_hash,
            "signer_id": self.signer_id,
            "receiver_id": self.receiver_id,
            "block_hash": self.block_hash,
            "explorer_url": self.explorer_url,
            "logs": self.logs,
            "gas_burnt": self.gas_burnt,
        }


class NearRpc:
    """JSON-RPC client with endpoint failover."""

    def __init__(self, network: str = "testnet", rpc_url: Optional[str] = None) -> None:
        if network not in NETWORKS:
            raise ValueError(f"Unknown NEAR network: {network}")
        self.network = network
        configured = rpc_url or os.environ.get("NEAR_RPC_URL")
        self.endpoints: List[str] = ([configured] if configured else []) + [
            u for u in NETWORKS[network]["rpc"] if u != configured
        ]
        self.explorer = NETWORKS[network]["explorer"]

    def call(self, method: str, params: Any, timeout: float = 30.0) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": "mend", "method": method, "params": params}).encode()
        last_error: Optional[str] = None
        for endpoint in self.endpoints:
            for attempt in range(3):
                req = urllib.request.Request(
                    endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"
                )
                try:
                    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    last_error = f"{endpoint}: HTTP {exc.code}"
                    if exc.code in (408, 429, 500, 502, 503, 504):
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    break
                except Exception as exc:  # network errors: try again, then the next endpoint
                    last_error = f"{endpoint}: {exc}"
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if "error" in payload:
                    err = payload["error"]
                    cause = (err.get("cause") or {}).get("name", "")
                    if cause == "TIMEOUT_ERROR":
                        last_error = f"{endpoint}: RPC timeout"
                        time.sleep(1.0)
                        continue
                    raise NearError(_format_rpc_error(err))
                return payload["result"]
        raise NearError(f"NEAR RPC unreachable ({last_error})")

    def status(self) -> Dict[str, Any]:
        result = self.call("status", [], timeout=10.0)
        sync = result.get("sync_info", {})
        return {
            "chain_id": result.get("chain_id"),
            "latest_block_height": sync.get("latest_block_height"),
            "latest_block_hash": sync.get("latest_block_hash"),
            "syncing": sync.get("syncing"),
        }

    def final_block_hash(self) -> str:
        return self.call("block", {"finality": "final"})["header"]["hash"]

    def view_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.call(
                "query", {"request_type": "view_account", "finality": "optimistic", "account_id": account_id}
            )
        except NearError as exc:
            if "UNKNOWN_ACCOUNT" in str(exc) or "does not exist" in str(exc):
                return None
            raise

    def access_key_nonce(self, account_id: str, public_key: str) -> int:
        result = self.call(
            "query",
            {
                "request_type": "view_access_key",
                "finality": "optimistic",
                "account_id": account_id,
                "public_key": public_key,
            },
        )
        return int(result["nonce"])

    def view(self, contract_id: str, method: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """Calls a read-only contract method and decodes its JSON return value."""
        encoded = base64.b64encode(json.dumps(args or {}).encode()).decode()
        result = self.call(
            "query",
            {
                "request_type": "call_function",
                "finality": "optimistic",
                "account_id": contract_id,
                "method_name": method,
                "args_base64": encoded,
            },
        )
        raw = bytes(result.get("result", []))
        return json.loads(raw.decode("utf-8")) if raw else None

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer}/txns/{tx_hash}"

    def account_url(self, account_id: str) -> str:
        return f"{self.explorer}/address/{account_id}"


def _format_rpc_error(err: Dict[str, Any]) -> str:
    cause = err.get("cause") or {}
    name = cause.get("name") or err.get("name") or "RPC_ERROR"
    detail = cause.get("info") or err.get("data") or err.get("message") or ""
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    return f"{name}: {detail}"[:600]


def _failure_message(failure: Any) -> str:
    """Pulls the contract panic text out of a nested Failure object."""
    text = json.dumps(failure)
    try:
        kind = failure["ActionError"]["kind"]
        if "FunctionCallError" in kind:
            inner = kind["FunctionCallError"]
            text = inner.get("ExecutionError") or json.dumps(inner)
        else:
            text = json.dumps(kind)
    except (KeyError, TypeError):
        pass
    return text[:600]


class NearAccount:
    """An account plus the key Mend signs with for it."""

    def __init__(self, account_id: str, key: KeyPair, rpc: NearRpc) -> None:
        self.account_id = account_id
        self.key = key
        self.rpc = rpc
        self._lock = threading.Lock()

    def send(self, receiver_id: str, actions: Sequence[bytes], wait_until: str = "EXECUTED_OPTIMISTIC") -> TxResult:
        """Signs and submits a transaction, raising NearError if it fails on-chain."""
        with self._lock:
            for attempt in range(3):
                nonce = self.rpc.access_key_nonce(self.account_id, self.key.public_key) + 1
                block_hash = self.rpc.final_block_hash()
                tx = (
                    _string(self.account_id)
                    + _public_key(self.key.public_bytes)
                    + _u64(nonce)
                    + _string(receiver_id)
                    + b58decode(block_hash)
                    + _u32(len(actions))
                    + b"".join(actions)
                )
                digest = hashlib.sha256(tx).digest()
                signed = tx + _u8(0) + self.key.sign(digest)
                try:
                    result = self.rpc.call(
                        "send_tx",
                        {"signed_tx_base64": base64.b64encode(signed).decode(), "wait_until": wait_until},
                        timeout=60.0,
                    )
                except NearError as exc:
                    # Another transaction from this key landed first: refetch the nonce.
                    if "InvalidNonce" in str(exc) and attempt < 2:
                        time.sleep(0.6)
                        continue
                    raise
                return self._parse(result, receiver_id, fallback_hash=b58encode(digest))
        raise NearError("Could not submit transaction after retrying the nonce")

    def _parse(self, result: Dict[str, Any], receiver_id: str, fallback_hash: str) -> TxResult:
        tx_hash = (result.get("transaction") or {}).get("hash") or fallback_hash
        outcomes = [result.get("transaction_outcome") or {}] + list(result.get("receipts_outcome") or [])
        logs: List[str] = []
        gas = 0
        for outcome in outcomes:
            body = outcome.get("outcome") or {}
            logs.extend(body.get("logs") or [])
            gas += int(body.get("gas_burnt") or 0)
            status = body.get("status")
            if isinstance(status, dict) and "Failure" in status:
                raise NearError(f"Transaction {tx_hash} failed: {_failure_message(status['Failure'])}")
        status = result.get("status")
        if isinstance(status, dict) and "Failure" in status:
            raise NearError(f"Transaction {tx_hash} failed: {_failure_message(status['Failure'])}")
        value: Any = None
        if isinstance(status, dict) and status.get("SuccessValue"):
            raw = base64.b64decode(status["SuccessValue"])
            try:
                value = json.loads(raw.decode("utf-8"))
            except ValueError:
                value = raw
        block_hash = (result.get("transaction_outcome") or {}).get("block_hash", "")
        return TxResult(
            tx_hash=tx_hash,
            signer_id=self.account_id,
            receiver_id=receiver_id,
            block_hash=block_hash,
            explorer_url=self.rpc.tx_url(tx_hash),
            logs=logs,
            return_value=value,
            gas_burnt=gas,
        )

    def call(
        self,
        contract_id: str,
        method: str,
        args: Dict[str, Any],
        deposit: int = 0,
        gas: int = DEFAULT_GAS,
        wait_until: str = "EXECUTED_OPTIMISTIC",
    ) -> TxResult:
        return self.send(contract_id, [function_call(method, args, gas, deposit)], wait_until=wait_until)

    def balance(self) -> int:
        account = self.rpc.view_account(self.account_id)
        return int(account["amount"]) if account else 0
