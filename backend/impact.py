"""ESG impact accounting.

The submitted report says "AI performs automatic ESG verification". That is not a
well-posed machine-learning problem: there is no labelled corpus of compliant
versus non-compliant energy production, so a model output would be an unauditable
opinion. Worse, a regulator cannot accept an impact figure that a neural network
produced and nobody can reproduce.

So impact here is **deterministic and published**:

    avoided CO2e (kg) = verified kWh x grid emission factor (kg CO2e / kWh)

Every record carries the method version and the emission-factor source, so any
figure can be recomputed by a third party years later. Machine learning is used
where it belongs - detecting anomalies in the data feeding this calculation - not
for deciding the number itself.

**Double counting.** If the operator already sold the renewable certificate
(I-REC or Guarantee of Origin) for the same MWh, then a token holder also claiming
the avoided CO2 is claiming it twice. That is the textbook definition of
greenwashing, and it is the failure mode a project built on verified green claims
cannot afford. Every impact record therefore carries an explicit certificate
status, and impact is only reported as *claimable* when the certificate is
retired to this platform or verifiably unissued.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

METHOD_VERSION = "ef-location-based-v1"


class CertificateStatus(str, Enum):
    """Who holds the environmental attribute for this energy."""

    RETIRED_TO_PLATFORM = "retired_to_platform"  # attribute is ours, claimable
    UNISSUED = "unissued"  # no certificate exists, claimable
    SOLD_ELSEWHERE = "sold_elsewhere"  # attribute already sold - NOT claimable
    UNKNOWN = "unknown"  # unverified - NOT claimable


CLAIMABLE_STATUSES = {
    CertificateStatus.RETIRED_TO_PLATFORM,
    CertificateStatus.UNISSUED,
}


@dataclass(frozen=True)
class ImpactResult:
    verified_kwh: float
    rejected_kwh: float
    emission_factor_kg_per_kwh: float
    emission_factor_source: str
    co2e_avoided_kg: float
    certificate_status: CertificateStatus
    is_claimable: bool
    method_version: str
    note: str

    def as_dict(self) -> dict:
        return {
            "verified_kwh": round(self.verified_kwh, 4),
            "rejected_kwh": round(self.rejected_kwh, 4),
            "emission_factor_kg_per_kwh": self.emission_factor_kg_per_kwh,
            "emission_factor_source": self.emission_factor_source,
            "co2e_avoided_kg": round(self.co2e_avoided_kg, 3),
            "certificate_status": self.certificate_status.value,
            "is_claimable": self.is_claimable,
            "method_version": self.method_version,
            "note": self.note,
        }


def compute_impact(
    verified_wh: float,
    rejected_wh: float,
    emission_factor_kg_per_kwh: float,
    emission_factor_source: str,
    certificate_status: CertificateStatus = CertificateStatus.UNKNOWN,
) -> ImpactResult:
    """Compute avoided emissions from **verified** energy only.

    Readings that failed validation contribute nothing. That is the point of
    ordering validation before accounting: an unverified kWh has no impact value,
    because the platform cannot stand behind it.
    """
    verified_kwh = max(0.0, verified_wh) / 1000.0
    rejected_kwh = max(0.0, rejected_wh) / 1000.0
    is_claimable = certificate_status in CLAIMABLE_STATUSES
    co2e = verified_kwh * emission_factor_kg_per_kwh

    if not is_claimable:
        note = (
            "Reported for information only. The environmental attribute for this "
            "energy is not held by the platform, so it is not claimable by token "
            "holders without double counting."
        )
    else:
        note = (
            "Claimable: the environmental attribute is retired to this platform or "
            "no certificate was issued for this energy."
        )

    return ImpactResult(
        verified_kwh=verified_kwh,
        rejected_kwh=rejected_kwh,
        emission_factor_kg_per_kwh=emission_factor_kg_per_kwh,
        emission_factor_source=emission_factor_source,
        co2e_avoided_kg=co2e,
        certificate_status=certificate_status,
        is_claimable=is_claimable,
        method_version=METHOD_VERSION,
        note=note,
    )
