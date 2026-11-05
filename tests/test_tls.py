import ssl
from datetime import datetime, timedelta, timezone

from cryptography import x509

from live_tracker import tls
from live_tracker.tls import ensure_certificate

ADDRESSES = ["127.0.0.1", "192.168.0.19"]


def _names(path) -> set[str]:
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(address) for address in names.get_values_for_type(x509.IPAddress)}


def test_the_certificate_covers_the_addresses_and_a_server_can_use_it(tmp_path):
    certificate, key = ensure_certificate(tmp_path / "tls", ADDRESSES)
    assert _names(certificate) == set(ADDRESSES)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)


def test_the_same_certificate_stays_while_it_covers_the_addresses(tmp_path):
    certificate, _ = ensure_certificate(tmp_path, ADDRESSES)
    first = certificate.read_bytes()
    ensure_certificate(tmp_path, ADDRESSES[:1])
    assert certificate.read_bytes() == first  # a browser that accepted it isn't asked again


def test_a_new_address_gets_a_new_certificate(tmp_path):
    certificate, _ = ensure_certificate(tmp_path, ADDRESSES)
    ensure_certificate(tmp_path, ["127.0.0.1", "192.168.0.20"])
    assert _names(certificate) == {"127.0.0.1", "192.168.0.20"}


def test_a_certificate_about_to_expire_is_made_again(tmp_path, monkeypatch):
    monkeypatch.setattr(tls, "VALID_DAYS", tls.RENEW_BEFORE_DAYS - 1)
    certificate, _ = ensure_certificate(tmp_path, ADDRESSES)
    first = certificate.read_bytes()
    monkeypatch.undo()
    ensure_certificate(tmp_path, ADDRESSES)
    assert certificate.read_bytes() != first
    expires = x509.load_pem_x509_certificate(certificate.read_bytes()).not_valid_after_utc
    assert expires > datetime.now(timezone.utc) + timedelta(days=tls.RENEW_BEFORE_DAYS)


def test_a_damaged_certificate_is_made_again(tmp_path):
    certificate, key = ensure_certificate(tmp_path, ADDRESSES)
    certificate.write_bytes(b"not a certificate")
    ensure_certificate(tmp_path, ADDRESSES)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)
