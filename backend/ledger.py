"""Fractional tokenisation and revenue distribution - off-chain reference.

This is a faithful Python implementation of the semantics the Stage 3 Solidity
contracts will have. Building it first is deliberate: the economics can be tested
and demonstrated with no chain, no gas, no wallet and no money, and the contract
then has an executable specification to match.

**Fractional ownership** (``AssetToken``, ERC-1155 semantics). One token id per
asset, a fixed supply representing 100% of the asset's distributable revenue. A
holder's entitlement is ``balance / total_supply``. At a 50 USD minimum and a
125,000 USD asset that is 2,500 tokens - each token is one 2,500th of the array.

**Pull-based accrual** (``RevenueVault``). The submitted report promises
"instant automatic distribution" to every holder. Pushing a payment to N holders
in one transaction is O(N) gas, exceeds the block limit as holders grow, and
breaks permanently if a single recipient reverts - one bad address blocks
everyone. The correct pattern, and the one implemented here, is accrual:

    accRevenuePerToken += deposit / totalSupply        (once per revenue event)
    owed(holder) = balance * accRevenuePerToken - debt(holder)

Distribution is then **O(1) per holder and independent of holder count**, and a
failing recipient cannot block anyone else. Transfers settle accrued revenue
before moving balance, so entitlement always follows the holder who owned the
tokens when the energy was produced.

Amounts are held as integers in micro-USDC (1e-6) exactly as the ERC-20 contract
will, so the rounding behaviour of the prototype matches the chain rather than
drifting from it. **No real money is involved anywhere in this module.**
"""

from __future__ import annotations

from dataclasses import dataclass, field

MICRO = 1_000_000  # 1 USDC = 1_000_000 micro-USDC, matching USDC's 6 decimals
# Fixed-point precision for the accumulator. Without this, integer division of a
# small deposit by a large supply truncates to zero and revenue silently vanishes.
ACC_PRECISION = 10**18


class LedgerError(Exception):
    pass


@dataclass
class Holder:
    address: str
    balance: int = 0
    reward_debt: int = 0  # balance * accPerToken at last settlement
    accrued_micro_usdc: int = 0  # settled but unclaimed
    claimed_micro_usdc: int = 0


@dataclass
class AssetLedger:
    """One tokenised asset: supply, holders and the revenue accumulator."""

    asset_id: str
    total_supply: int
    token_price_micro_usdc: int
    acc_per_token: int = 0  # scaled by ACC_PRECISION
    total_deposited_micro_usdc: int = 0
    total_claimed_micro_usdc: int = 0
    undistributed_micro_usdc: int = 0  # deposits made while supply was unheld
    holders: dict[str, Holder] = field(default_factory=dict)
    paused: bool = False
    pause_reason: str = ""

    # -- helpers ------------------------------------------------------------

    def _holder(self, address: str) -> Holder:
        if address not in self.holders:
            self.holders[address] = Holder(address=address)
        return self.holders[address]

    def _pending(self, holder: Holder) -> int:
        return (holder.balance * self.acc_per_token) // ACC_PRECISION - holder.reward_debt

    def _settle(self, holder: Holder) -> None:
        """Move accrued-but-unsettled revenue into the holder's settled balance."""
        pending = self._pending(holder)
        if pending > 0:
            holder.accrued_micro_usdc += pending
        holder.reward_debt = (holder.balance * self.acc_per_token) // ACC_PRECISION

    @property
    def circulating_supply(self) -> int:
        return sum(h.balance for h in self.holders.values())

    @property
    def unsold_supply(self) -> int:
        return self.total_supply - self.circulating_supply

    # -- operations ---------------------------------------------------------

    def purchase(self, address: str, token_count: int) -> Holder:
        """Buy fractional tokens. Mirrors a KYC-gated ERC-1155 mint."""
        if self.paused:
            raise LedgerError(f"Asset is paused: {self.pause_reason}")
        if token_count <= 0:
            raise LedgerError("token_count must be positive")
        if token_count > self.unsold_supply:
            raise LedgerError(
                f"Only {self.unsold_supply} tokens remain unsold"
            )

        holder = self._holder(address)
        self._settle(holder)
        holder.balance += token_count
        holder.reward_debt = (holder.balance * self.acc_per_token) // ACC_PRECISION
        return holder

    def transfer(self, sender: str, recipient: str, token_count: int) -> None:
        """Transfer tokens, settling both sides first.

        Settling before moving balance is what makes accrual correct: the seller
        keeps revenue earned while they held the tokens, and the buyer starts
        accruing only from now.
        """
        if self.paused:
            raise LedgerError(f"Asset is paused: {self.pause_reason}")
        source = self._holder(sender)
        if source.balance < token_count:
            raise LedgerError("Insufficient token balance")

        target = self._holder(recipient)
        self._settle(source)
        self._settle(target)
        source.balance -= token_count
        target.balance += token_count
        source.reward_debt = (source.balance * self.acc_per_token) // ACC_PRECISION
        target.reward_debt = (target.balance * self.acc_per_token) // ACC_PRECISION

    def deposit_revenue(self, amount_micro_usdc: int) -> int:
        """Distribute a revenue event across all holders in O(1).

        Returns the amount actually distributed. Revenue attributable to unsold
        tokens is held in ``undistributed`` rather than being silently spread over
        existing holders, which would over-pay early investors.
        """
        if amount_micro_usdc <= 0:
            return 0

        circulating = self.circulating_supply
        self.total_deposited_micro_usdc += amount_micro_usdc

        if circulating == 0:
            self.undistributed_micro_usdc += amount_micro_usdc
            return 0

        distributable = (amount_micro_usdc * circulating) // self.total_supply
        self.undistributed_micro_usdc += amount_micro_usdc - distributable
        self.acc_per_token += (distributable * ACC_PRECISION) // circulating
        return distributable

    def claimable(self, address: str) -> int:
        holder = self.holders.get(address)
        if holder is None:
            return 0
        return holder.accrued_micro_usdc + self._pending(holder)

    def claim(self, address: str) -> int:
        """Holder pulls their revenue. Cannot be blocked by any other holder."""
        if self.paused:
            raise LedgerError(f"Asset is paused: {self.pause_reason}")
        holder = self.holders.get(address)
        if holder is None:
            raise LedgerError(f"Unknown holder {address}")

        self._settle(holder)
        amount = holder.accrued_micro_usdc
        if amount <= 0:
            return 0

        holder.accrued_micro_usdc = 0
        holder.claimed_micro_usdc += amount
        self.total_claimed_micro_usdc += amount
        return amount

    def pause(self, reason: str) -> None:
        """Halt distribution when the data feeding it stops being trustworthy.

        This is the link back to the validation layer: untrusted telemetry stops
        money moving. Trust is a precondition of payment.
        """
        self.paused = True
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""

    # -- reporting ----------------------------------------------------------

    def holder_view(self, address: str) -> dict:
        holder = self.holders.get(address) or Holder(address=address)
        share = holder.balance / self.total_supply if self.total_supply else 0.0
        return {
            "address": address,
            "token_balance": holder.balance,
            "ownership_percent": round(share * 100.0, 6),
            "investment_usdc": round(
                holder.balance * self.token_price_micro_usdc / MICRO, 2
            ),
            "claimable_usdc": round(self.claimable(address) / MICRO, 6),
            "claimed_usdc": round(holder.claimed_micro_usdc / MICRO, 6),
        }

    def summary(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "total_supply": self.total_supply,
            "circulating_supply": self.circulating_supply,
            "unsold_supply": self.unsold_supply,
            "token_price_usdc": round(self.token_price_micro_usdc / MICRO, 2),
            "holder_count": sum(1 for h in self.holders.values() if h.balance > 0),
            "total_distributed_usdc": round(
                self.total_deposited_micro_usdc / MICRO, 6
            ),
            "total_claimed_usdc": round(self.total_claimed_micro_usdc / MICRO, 6),
            "undistributed_usdc": round(self.undistributed_micro_usdc / MICRO, 6),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }


def revenue_from_energy(
    verified_wh: float,
    tariff_micro_usdc_per_kwh: int,
    platform_fee_bps: int,
    opex_bps: int,
) -> dict:
    """Turn verified energy into a distributable amount.

    The submitted report distributes gross revenue, which is not a thing that
    exists: an operating plant has maintenance, insurance and inverter
    replacement. Operating cost is deducted here before anything is distributed,
    and the platform fee is taken from the gross, matching the 0.5-1.5% band in
    the business model.
    """
    gross = int((max(0.0, verified_wh) / 1000.0) * tariff_micro_usdc_per_kwh)
    platform_fee = gross * platform_fee_bps // 10_000
    opex = gross * opex_bps // 10_000
    net = max(0, gross - platform_fee - opex)
    return {
        "gross_micro_usdc": gross,
        "platform_fee_micro_usdc": platform_fee,
        "opex_micro_usdc": opex,
        "net_micro_usdc": net,
    }
