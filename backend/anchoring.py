"""Merkle batch anchoring.

Writing every reading to a blockchain would be absurd: one device at 5-second
resolution is 17,280 transactions a day. Instead, validated readings are batched
per interval, hashed into a Merkle tree, and only the 32-byte **root** is
published. Anyone can then prove that a specific reading belongs to an anchored
batch with a proof of log2(n) hashes - roughly 11 hashes for a 2,000-reading
batch - without the chain ever storing the readings.

This module is deliberately chain-agnostic: it produces roots and proofs. Stage 3
writes those roots to an ``AnchorRegistry`` contract on Polygon Amoy; until then
they are recorded in a local append-only anchor log with the same interface. The
cryptography does not change when the destination does, which is exactly why the
anchoring is worth building before the chain.

Pure standard library (``hashlib``): no dependency, no cost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def leaf_hash(payload_hash_hex: str) -> bytes:
    """Domain-separated leaf hash.

    The 0x00 prefix stops an internal node from ever being presented as a leaf -
    the classic second-preimage attack on naive Merkle trees.
    """
    return hashlib.sha256(b"\x00" + bytes.fromhex(payload_hash_hex)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


@dataclass(frozen=True)
class MerkleProof:
    leaf_hash_hex: str
    root_hex: str
    path: list[tuple[str, str]]  # (sibling_hash_hex, "left" | "right")

    def as_dict(self) -> dict:
        return {
            "leaf": self.leaf_hash_hex,
            "root": self.root_hex,
            "path": [{"sibling": s, "position": p} for s, p in self.path],
            "proof_length": len(self.path),
        }


class MerkleTree:
    """Binary Merkle tree over reading payload hashes.

    An odd node at any level is promoted unchanged to the next level, which keeps
    the implementation short and matches the layout the Solidity verifier will use.
    """

    def __init__(self, payload_hashes: list[str]) -> None:
        if not payload_hashes:
            raise ValueError("cannot build a Merkle tree over zero readings")
        self.leaves: list[bytes] = [leaf_hash(h) for h in payload_hashes]
        self.levels: list[list[bytes]] = [self.leaves]
        self._build()

    def _build(self) -> None:
        level = self.leaves
        while len(level) > 1:
            nxt: list[bytes] = []
            for i in range(0, len(level) - 1, 2):
                nxt.append(node_hash(level[i], level[i + 1]))
            if len(level) % 2 == 1:
                nxt.append(level[-1])
            self.levels.append(nxt)
            level = nxt

    @property
    def root(self) -> bytes:
        return self.levels[-1][0]

    @property
    def root_hex(self) -> str:
        return self.root.hex()

    def proof_for_index(self, index: int) -> MerkleProof:
        if not 0 <= index < len(self.leaves):
            raise IndexError(f"leaf index {index} out of range")

        path: list[tuple[str, str]] = []
        position = index
        for level in self.levels[:-1]:
            if position % 2 == 0:
                sibling_index = position + 1
                side = "right"
            else:
                sibling_index = position - 1
                side = "left"
            if sibling_index < len(level):
                path.append((level[sibling_index].hex(), side))
            position //= 2

        return MerkleProof(
            leaf_hash_hex=self.leaves[index].hex(), root_hex=self.root_hex, path=path
        )


def verify_proof(payload_hash_hex: str, proof: MerkleProof) -> bool:
    """Recompute the root from a leaf and its path.

    This is the function a third party runs to check a payout. It is also the
    exact algorithm the Solidity verifier will implement in Stage 3.
    """
    try:
        current = leaf_hash(payload_hash_hex)
    except ValueError:
        return False

    if current.hex() != proof.leaf_hash_hex:
        return False

    for sibling_hex, position in proof.path:
        sibling = bytes.fromhex(sibling_hex)
        current = (
            node_hash(sibling, current) if position == "left" else node_hash(current, sibling)
        )

    return current.hex() == proof.root_hex


def mock_transaction_hash(asset_id: str, root_hex: str, interval_start: str) -> str:
    """Deterministic stand-in for a Polygon transaction hash.

    Stage 1 has no chain, and inventing a random hex string that looks like a real
    transaction would be dishonest. This value is derived from the batch content,
    is labelled ``local-anchor`` everywhere it surfaces, and is replaced by the
    real Amoy transaction hash in Stage 3 with no schema change.
    """
    digest = hashlib.sha256(
        f"local-anchor|{asset_id}|{interval_start}|{root_hex}".encode("utf-8")
    ).hexdigest()
    return f"0x{digest}"
