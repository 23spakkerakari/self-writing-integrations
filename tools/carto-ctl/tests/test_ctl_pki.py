"""carto-ctl pki init (spec 14.4): a CA plus one server+client pair per service, the expected
SANs and EKUs, owner-only keys, fingerprints on stdout and never a key, refusal to overwrite."""

from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from carto_common.pki import certificate_fingerprint, load_certificate, load_private_key
from carto_ctl.cli import main
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from carto_ctl.pki import DEFAULT_SERVICES, initialize_pki


def _signed_by(leaf: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    public_key.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))


def _files(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def test_pki_init_writes_the_ca_and_one_pair_per_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    argv = ["pki", "init", "--out", str(out), "--dns", "core.internal", "--ip", "10.0.0.5"]
    assert main(argv) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    ca = load_certificate(out / "ca.crt")
    load_private_key(out / "ca.key")
    assert certificate_fingerprint(ca) in captured.out
    expected_files = {"ca.crt", "ca.key"}
    for service in DEFAULT_SERVICES:
        expected_files |= {f"{service}.crt", f"{service}.key"}
        leaf = load_certificate(out / f"{service}.crt")
        load_private_key(out / f"{service}.key")
        assert leaf.issuer == ca.subject
        _signed_by(leaf, ca)
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert sans.get_values_for_type(x509.DNSName) == [service, "localhost", "core.internal"]
        assert [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]
        ekus = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.SERVER_AUTH in ekus
        assert ExtendedKeyUsageOID.CLIENT_AUTH in ekus
        assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == dt.timedelta(days=90)
        assert f"{service}.crt  {certificate_fingerprint(leaf)}" in captured.out
    assert _files(out) == expected_files
    assert "-----BEGIN" not in captured.out
    assert "PRIVATE" not in captured.out


def test_ca_key_dir_keeps_the_ca_key_away_from_the_service_files(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    ca_dir = tmp_path / "pki-ca"
    argv = ["pki", "init", "--out", str(out), "--ca-key-dir", str(ca_dir)]
    assert main(argv) == EXIT_OK
    assert "ca.key" not in _files(out)
    assert _files(ca_dir) == {"ca.key"}
    ca = load_certificate(out / "ca.crt")
    key = load_private_key(ca_dir / "ca.key")
    assert key.public_key().public_numbers() == ca.public_key().public_numbers()  # type: ignore[union-attr]


def test_if_missing_succeeds_on_a_complete_run_and_refuses_a_partial_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    ca_dir = tmp_path / "pki-ca"
    argv = ["pki", "init", "--out", str(out), "--ca-key-dir", str(ca_dir), "--if-missing"]
    assert main(argv) == EXIT_OK
    first = {name: (out / name).read_bytes() for name in _files(out)}
    capsys.readouterr()
    assert main(argv) == EXIT_OK  # the Compose job on its second start
    assert "nothing written" in capsys.readouterr().out
    assert {name: (out / name).read_bytes() for name in _files(out)} == first
    (out / "edge-gateway.crt").unlink()  # a partial state is never papered over
    assert main(argv) == EXIT_FAILURE
    assert "refusing to overwrite" in capsys.readouterr().err
    assert main(["pki", "init", "--out", str(out), "--ca-key-dir", str(ca_dir)]) == EXIT_FAILURE


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_private_keys_are_owner_only(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out)]) == EXIT_OK
    for key in out.glob("*.key"):
        assert stat.S_IMODE(key.stat().st_mode) == 0o600, key


def test_pki_init_refuses_to_overwrite_any_existing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out)]) == EXIT_OK
    before = {path.name: path.read_bytes() for path in out.iterdir()}
    capsys.readouterr()
    assert main(["pki", "init", "--out", str(out)]) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to overwrite" in captured.err
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before
    # One stale service key is enough, and nothing is written before the check.
    other = tmp_path / "other"
    other.mkdir()
    (other / "api.key").write_bytes(b"stale")
    assert main(["pki", "init", "--out", str(other)]) == EXIT_FAILURE
    assert "api.key" in capsys.readouterr().err
    assert _files(other) == {"api.key"}


def test_custom_services_and_lifetime(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    argv = [
        "pki",
        "init",
        "--out",
        str(out),
        "--services",
        "edge-gateway,ingest-api",
        "--days",
        "30",
    ]
    assert main(argv) == EXIT_OK
    assert _files(out) == {
        "ca.crt",
        "ca.key",
        "edge-gateway.crt",
        "edge-gateway.key",
        "ingest-api.crt",
        "ingest-api.key",
    }
    leaf = load_certificate(out / "ingest-api.crt")
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == dt.timedelta(days=30)
    ca = load_certificate(out / "ca.crt")
    assert ca.not_valid_after_utc - ca.not_valid_before_utc == dt.timedelta(days=3650)


@pytest.mark.parametrize(
    "argv",
    [
        ["--services", "Edge"],
        ["--services", "a,,b"],
        ["--services", "-edge"],
        ["--ip", "nope"],
        ["--days", "0"],
        ["--days", "4000"],
        ["--days", "ninety"],
        ["--dns", "bad name"],
        ["--dns", "-core.internal"],
    ],
)
def test_bad_options_are_usage_errors_and_write_nothing(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out), *argv]) == EXIT_USAGE
    assert not out.exists()
    assert "usage:" in capsys.readouterr().err


def test_default_out_is_pki_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["pki", "init"]) == EXIT_OK
    assert (tmp_path / "pki" / "ca.crt").is_file()
    assert "pki" in capsys.readouterr().out


def test_initialize_pki_dedupes_san_names_and_keeps_order(tmp_path: Path) -> None:
    result = initialize_pki(
        tmp_path / "p", ("api",), ("localhost", "api", "x.internal"), ("127.0.0.1",), 90
    )
    assert [service for service, _fp in result.services] == ["api"]
    leaf = load_certificate(tmp_path / "p" / "api.crt")
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["api", "localhost", "x.internal"]
    assert [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


def test_help_lists_the_options_and_no_longer_says_it_is_a_stub(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["pki", "init", "--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for option in ("--out", "--services", "--dns", "--ip", "--days"):
        assert option in out
    assert "Arrives in" not in out
    assert "(spec 14.4)" in out
