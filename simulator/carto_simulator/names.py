"""Synthetic names, addresses and leak-test markers (spec 0.1 item 7, 18.3).

Every value here is invented. Emails and addresses embed a marker token ``mk<8 lowercase hex>``
drawn from the scenario RNG so the leak test (spec 18.3) can scan for it; the token is unique per
value and embedded unchanged, so ``token in value`` holds for every recorded token
(:class:`~carto_simulator.ground_truth.MarkerSet`). Names come from fixed lists so phonetic forms
(spec 8.4) are stable across runs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

FIRST_NAMES: tuple[str, ...] = (
    "Jane",
    "John",
    "Maria",
    "Wei",
    "Aisha",
    "Carlos",
    "Priya",
    "Liam",
    "Olivia",
    "Noah",
    "Emma",
    "Lucas",
    "Sofia",
    "Mateo",
    "Amara",
    "Ethan",
    "Hana",
    "Daniel",
    "Fatima",
    "Victor",
    "Nora",
    "Samuel",
    "Chloe",
    "Omar",
    "Isabel",
    "Arjun",
    "Grace",
    "Tomas",
    "Yuki",
    "Elena",
    "Marcus",
    "Leila",
    "Felix",
    "Ingrid",
    "Rafael",
    "Zara",
    "Henry",
    "Mei",
    "Oscar",
    "Ruth",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith",
    "Johnson",
    "Garcia",
    "Chen",
    "Okafor",
    "Martinez",
    "Patel",
    "Nguyen",
    "Walsh",
    "Kim",
    "Lopez",
    "Brown",
    "Miller",
    "Schmidt",
    "Rossi",
    "Novak",
    "Haddad",
    "Kowalski",
    "Fischer",
    "Silva",
    "Andersen",
    "Dubois",
    "Moreau",
    "Ivanova",
    "Tanaka",
    "Sato",
    "Mensah",
    "Abebe",
    "Costa",
    "Larsen",
    "Murphy",
    "O'Brien",
    "Reyes",
    "Castillo",
    "Khan",
    "Singh",
    "Jensen",
    "Berg",
    "Lindqvist",
    "Varga",
)

CITIES: tuple[tuple[str, str], ...] = (
    ("Austin", "TX"),
    ("Denver", "CO"),
    ("Portland", "OR"),
    ("Columbus", "OH"),
    ("Raleigh", "NC"),
    ("Boise", "ID"),
    ("Tucson", "AZ"),
    ("Madison", "WI"),
    ("Richmond", "VA"),
    ("Omaha", "NE"),
    ("Tampa", "FL"),
    ("Albany", "NY"),
)

CLERKS: tuple[str, ...] = (
    "jsmith",
    "mlopez",
    "rchen",
    "apatel",
    "dkim",
    "lnguyen",
    "bwalsh",
    "tokafor",
)

SERVICE_ACCOUNT = "svc_wms_integration"

MARKER_PREFIX = "mk"
MARKER_HEX_LENGTH = 8


@dataclass(frozen=True, slots=True)
class Person:
    """One synthetic customer: a name plus the marker-bearing contact fields."""

    name: str
    email: str
    email_marker: str
    address: str
    address_marker: str


class MarkerFactory:
    """Draws unique marker tokens from the scenario RNG (spec 18.3)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._seen: set[str] = set()
        self.tokens: list[str] = []

    def new_token(self) -> str:
        """Return ``mk`` + 8 lowercase hex digits never returned before by this factory."""
        while True:
            value = self._rng.getrandbits(4 * MARKER_HEX_LENGTH)
            token = f"{MARKER_PREFIX}{value:0{MARKER_HEX_LENGTH}x}"
            if token not in self._seen:
                self._seen.add(token)
                self.tokens.append(token)
                return token


def _email_local_part(name: str) -> str:
    first, _, last = name.partition(" ")
    return "".join(ch for ch in f"{first}.{last}".lower() if ch.isalpha() or ch == ".")


def draw_person(rng: random.Random, markers: MarkerFactory) -> Person:
    """Draw a name, an email and a shipping address, consuming the RNG in a fixed order."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    name = f"{first} {last}"
    email_marker = markers.new_token()
    email = f"{_email_local_part(name)}+{email_marker}@example.com"
    number = rng.randint(1, 9999)
    address_marker = markers.new_token()
    city, state = rng.choice(CITIES)
    zip_code = f"{rng.randint(0, 99999):05d}"
    address = f"{number} {address_marker} Street, {city}, {state} {zip_code}"
    return Person(
        name=name,
        email=email,
        email_marker=email_marker,
        address=address,
        address_marker=address_marker,
    )
