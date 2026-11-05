"""The certificate of the web UI's https port.

Browsers only give a page the camera over https, and a computer on a local network can't get a certificate they
trust, so it makes its own: a browser warns about it once, and works after that.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 3650
RENEW_BEFORE_DAYS = 30


def ensure_certificate(directory: Path, addresses: list[str]) -> tuple[Path, Path]:
    """The certificate and key files for these IP addresses, made again when they're missing, cover other addresses
    or are about to expire. A browser that accepted the old certificate then warns about the new one."""
    certificate, key = directory / "web.crt", directory / "web.key"
    if not key.is_file() or not _still_good(certificate, addresses):
        directory.mkdir(parents=True, exist_ok=True)
        _make(certificate, key, addresses)
    return certificate, key


def _still_good(path: Path, addresses: list[str]) -> bool:
    if not path.is_file():
        return False
    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except ValueError:
        return False
    except x509.ExtensionNotFound:
        return False
    renew_at = datetime.now(timezone.utc) + timedelta(days=RENEW_BEFORE_DAYS)
    covered = {str(address) for address in names.get_values_for_type(x509.IPAddress)}
    return certificate.not_valid_after_utc > renew_at and covered.issuperset(addresses)


def _make(certificate_path: Path, key_path: Path, addresses: list[str]) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Live Tracker")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))  # a tablet's clock may run behind
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)  # a device can install it to trust it
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
