"""Tests for Hive signature verification — hive_auth module."""
import hashlib
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from project.api.hive_auth import (
    _base58_decode, _decode_pubkey, verify_hive_signature, fetch_posting_keys,
)


# ── _base58_decode ──────────────────────────────────────────────────────────

class TestBase58Decode:
    def test_valid_decode(self):
        """Known base58 string decodes correctly."""
        # "1" in base58 is 0x00
        result = _base58_decode("1")
        assert result == b"\x00"

    def test_multi_char_decode(self):
        """Multi-character base58 produces bytes."""
        result = _base58_decode("2")
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_invalid_char_raises(self):
        """Invalid base58 character (0, O, I, l) raises ValueError."""
        with pytest.raises(ValueError):
            _base58_decode("0invalid")

    def test_leading_ones_preserved(self):
        """Leading '1's map to leading 0x00 bytes."""
        result = _base58_decode("111")
        assert result[:3] == b"\x00\x00\x00"


# ── _decode_pubkey ──────────────────────────────────────────────────────────

class TestDecodePubkey:
    def test_strips_stm_prefix(self):
        """STM prefix is stripped before decoding."""
        # Use a known short key — we just test that prefix stripping works.
        raw_no_prefix = _base58_decode("1111111111111111111111111114oLvT2")
        raw_with_prefix = _decode_pubkey("STM1111111111111111111111111114oLvT2")
        assert raw_with_prefix == raw_no_prefix[:-4]

    def test_no_prefix(self):
        """Key without STM prefix still decodes."""
        result = _decode_pubkey("1111111111111111111111111114oLvT2")
        assert isinstance(result, bytes)


# ── verify_hive_signature ──────────────────────────────────────────────────

class TestVerifyHiveSignature:
    def test_invalid_hex_returns_false(self):
        assert verify_hive_signature("msg", "not-hex", ["STM7abc"]) is False

    def test_wrong_length_returns_false(self):
        assert verify_hive_signature("msg", "aabb", ["STM7abc"]) is False

    def test_bad_recovery_flag_returns_false(self):
        # Recovery flag 0xff is outside valid range (27-34)
        sig = "ff" + "00" * 64
        assert verify_hive_signature("msg", sig, ["STM7abc"]) is False

    def test_recovery_failure_returns_false(self):
        """If ecdsa recovery raises, should return False (not crash)."""
        # Use a valid-looking signature with recovery flag in range but garbage data.
        sig = "1f" + "ab" * 64
        result = verify_hive_signature("test message", sig, ["STM7abc123"])
        assert result is False


# ── fetch_posting_keys ──────────────────────────────────────────────────────

class TestFetchPostingKeys:
    async def test_returns_keys_on_success(self):
        """Successful API call returns posting key list."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [{
                "posting": {"key_auths": [["STM7abc", 1]]}
            }]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("project.api.hive_auth.httpx.AsyncClient", return_value=mock_client):
            keys = await fetch_posting_keys("testuser")
        assert keys == ["STM7abc"]

    async def test_returns_empty_on_no_accounts(self):
        """Empty result returns empty list."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("project.api.hive_auth.httpx.AsyncClient", return_value=mock_client):
            keys = await fetch_posting_keys("nonexistent")
        assert keys == []

    async def test_falls_back_across_nodes(self):
        """If first node fails, tries next."""
        call_count = [0]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [{"posting": {"key_auths": [["STM7xyz", 1]]}}]
        }

        async def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("node down")
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("project.api.hive_auth.httpx.AsyncClient", return_value=mock_client):
            keys = await fetch_posting_keys("testuser")
        assert keys == ["STM7xyz"]
        assert call_count[0] == 2
