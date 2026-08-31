"""Deterministic receptionist directory and transfer intent resolution."""
from __future__ import annotations

import re
from dataclasses import dataclass


TRANSFER_INTENT = re.compile(
    r"\b(transfer|connect|speak|talk|put me through|looking for)\b"
    r"|\bis\b.+\b(there|available)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DirectoryPerson:
    extension: str
    full_name: str


@dataclass(frozen=True)
class TransferResolution:
    requested: bool
    matches: tuple[DirectoryPerson, ...] = ()


def resolve_transfer(text: str, people: tuple[DirectoryPerson, ...]) -> TransferResolution:
    """Resolve names only when the caller expresses transfer intent.

    Full names win. Otherwise a first or last name may match, with every
    matching person returned so the receptionist can ask for clarification.
    """
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    if not TRANSFER_INTENT.search(normalized):
        return TransferResolution(requested=False)

    full = tuple(
        person for person in people
        if " ".join(re.findall(r"[a-z0-9]+", person.full_name.lower())) in normalized
    )
    if full:
        return TransferResolution(requested=True, matches=full)

    words = set(normalized.split())
    partial = tuple(
        person for person in people
        if words.intersection(re.findall(r"[a-z0-9]+", person.full_name.lower()))
    )
    return TransferResolution(requested=True, matches=partial)
