"""Hive signature verification — verify Keychain requestSignBuffer signatures.

Hive uses secp256k1 ECDSA (same as Bitcoin). Keychain's requestSignBuffer
signs SHA256(message) and produces a recoverable signature (65 bytes:
1 byte recovery flag + 32 bytes r + 32 bytes s).

Public keys are base58check-encoded with an "STM" prefix.
"""
import hashlib
import hmac
import logging

import httpx
from ecdsa import SECP256k1, VerifyingKey
from ecdsa.util import sigdecode_string

logger = logging.getLogger(__name__)

_HIVE_API_NODES = [
    "https://api.hive.blog",
    "https://api.deathwing.me",
    "https://api.openhive.network",
]
_TIMEOUT = 4.0

_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    """Decode a base58-encoded string to bytes."""
    n = 0
    for ch in s.encode("ascii"):
        n = n * 58 + _ALPHABET.index(ch)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big")
    # Preserve leading zeros.
    pad = 0
    for ch in s.encode("ascii"):
        if ch == _ALPHABET[0]:
            pad += 1
        else:
            break
    return b"\x00" * pad + result


def _decode_pubkey(pubkey_str: str) -> bytes:
    """Decode a Hive STM-prefixed public key to raw 33-byte compressed point."""
    if pubkey_str.startswith("STM"):
        pubkey_str = pubkey_str[3:]
    raw = _base58_decode(pubkey_str)
    # Last 4 bytes are the RIPEMD160 checksum.
    return raw[:-4]


async def fetch_posting_keys(username: str) -> list[str]:
    """Fetch a user's posting public keys from the Hive API.

    Returns list of public key strings (e.g. ["STM7abc..."]).
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "condenser_api.get_accounts",
        "params": [[username]],
        "id": 1,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for node in _HIVE_API_NODES:
            try:
                resp = await client.post(node, json=payload)
                data = resp.json()
                accounts = data.get("result", [])
                if not accounts:
                    return []
                posting = accounts[0].get("posting", {})
                key_auths = posting.get("key_auths", [])
                return [ka[0] for ka in key_auths if ka]
            except Exception as exc:
                logger.debug("fetch_posting_keys from %s failed: %s", node, exc)
                continue
    return []


def verify_hive_signature(
    message: str, signature_hex: str, expected_pubkeys: list[str]
) -> bool:
    """Verify a Hive Keychain requestSignBuffer signature.

    The signature is a 65-byte recoverable ECDSA signature:
    - byte 0: recovery flag (27-30 for uncompressed, 31-34 for compressed)
    - bytes 1-32: r
    - bytes 33-64: s

    Returns True if the recovered public key matches any of expected_pubkeys.
    """
    try:
        sig_bytes = bytes.fromhex(signature_hex)
    except ValueError:
        return False

    if len(sig_bytes) != 65:
        return False

    recovery_flag = sig_bytes[0]
    r_s = sig_bytes[1:]
    digest = hashlib.sha256(message.encode("utf-8")).digest()

    # Recovery flag: 31-34 for compressed keys (Hive uses compressed).
    # Subtract 31 to get recovery index 0-3.
    if 31 <= recovery_flag <= 34:
        recid = recovery_flag - 31
    elif 27 <= recovery_flag <= 30:
        recid = recovery_flag - 27
    else:
        return False

    try:
        recovered_keys = VerifyingKey.from_public_key_recovery_with_digest(
            r_s, digest, SECP256k1, sigdecode=sigdecode_string
        )
    except Exception:
        return False

    if recid >= len(recovered_keys):
        return False

    recovered = recovered_keys[recid]
    recovered_compressed = recovered.to_string("compressed")

    for pubkey_str in expected_pubkeys:
        try:
            expected_bytes = _decode_pubkey(pubkey_str)
            if hmac.compare_digest(recovered_compressed, expected_bytes):
                return True
        except Exception:
            continue

    return False
