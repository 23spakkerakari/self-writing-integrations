"""Synthetic people, marker tokens, typos and draw helpers (spec 18.3 markers, 19 typos)."""

from __future__ import annotations

import random
import re

from carto_simulator.names import CLERKS, MarkerFactory, draw_person
from carto_simulator.scenarios.shop_model import apply_typo, po_number, poisson


def test_marker_tokens_are_unique_and_well_formed() -> None:
    factory = MarkerFactory(random.Random(1))
    tokens = [factory.new_token() for _ in range(5000)]
    assert len(set(tokens)) == len(tokens)
    assert all(re.fullmatch(r"mk[0-9a-f]{8}", token) for token in tokens)


def test_draw_person_shapes_and_determinism() -> None:
    first = draw_person(random.Random(5), MarkerFactory(random.Random(5)))
    second = draw_person(random.Random(5), MarkerFactory(random.Random(5)))
    assert first == second
    assert re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z']+", first.name)
    assert re.fullmatch(r"[a-z]+\.[a-z]+\+mk[0-9a-f]{8}@example\.com", first.email)
    assert first.email_marker in first.email
    assert re.fullmatch(r"\d{1,4} mk[0-9a-f]{8} Street, [A-Za-z ]+, [A-Z]{2} \d{5}", first.address)
    assert first.address_marker in first.address  # the MarkerSet contract: an exact substring
    assert first.email_marker != first.address_marker


def test_apostrophes_never_reach_email_local_parts() -> None:
    rng = random.Random(2)
    factory = MarkerFactory(rng)
    for _ in range(400):
        person = draw_person(rng, factory)
        assert "'" not in person.email


def test_apply_typo_breaks_the_digits_form() -> None:
    rng = random.Random(9)
    for order_id in [*range(4471, 4700), 1000000, 1111110, 7]:
        original = f"SO-{order_id:07d}"
        typo = apply_typo(rng, original)
        assert typo != original
        assert typo.startswith("SO-") and len(typo) == len(original)
        assert typo[3:].isdigit()
        assert int(typo[3:]) != order_id
        assert (
            sorted(typo[3:]) == sorted(original[3:])
            or sum(a != b for a, b in zip(typo[3:], original[3:], strict=True)) == 1
        )


def test_po_number_shape() -> None:
    assert po_number(88210) == "88-210"
    assert po_number(99999) == "99-999"
    assert po_number(100000) == "100-000"
    assert po_number(1005) == "01-005"


def test_poisson_mean() -> None:
    rng = random.Random(3)
    samples = [poisson(rng, 5.0) for _ in range(4000)]
    assert 4.7 < sum(samples) / len(samples) < 5.3
    assert poisson(rng, 0.0) == 0
    assert poisson(rng, 400.0) > 300


def test_clerks() -> None:
    assert len(CLERKS) == 8 and len(set(CLERKS)) == 8
    assert all(re.fullmatch(r"[a-z]{4,8}", clerk) for clerk in CLERKS)
