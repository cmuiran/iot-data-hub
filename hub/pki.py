"""OPC UA certificates: self-signed application certificates and trust lists.

    hub-admin make-cert --role client --name hub-client      # for the hub's OPC UA driver
    hub-admin make-cert --role server --name hub-server      # for the hub's OPC UA server

Files: ``<dir>/<name>.der`` (certificate, DER) and ``<dir>/<name>.pem`` (private key, PEM;
keep it secret). Copy the client ``.der`` into the PLC's trusted certificates (TIA Portal,
Kepware, ...) and the PLC's certificate into ``<pki>/trusted/`` when the hub must verify it.
The application URI is written into the certificate's subjectAltName; OPC UA peers compare it
with the URI the application announces, so the driver takes it from the certificate.
"""

from __future__ import annotations

import socket
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

DEFAULT_URIS = {"client": "urn:iot-data-hub:client", "server": "urn:iot-data-hub:server"}


async def make_certificate(directory: Path, name: str, role: str, *, uri: str | None = None,
                           hostname: str | None = None) -> tuple[Path, Path]:
    from asyncua.crypto.cert_gen import setup_self_signed_certificate

    if role not in ("client", "server"):
        raise ValueError("role must be client or server")
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / f"{name}.der", directory / f"{name}.pem"
    use = [ExtendedKeyUsageOID.CLIENT_AUTH] if role == "client" else [
        ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
    await setup_self_signed_certificate(
        key, cert, uri or DEFAULT_URIS[role], hostname or socket.gethostname(), use,
        {"countryName": "IR", "organizationName": "IoT Data Hub", "commonName": name})
    return cert, key


def application_uri(cert_path: Path | str) -> str | None:
    """The URI in a certificate's subjectAltName (DER or PEM)."""
    data = Path(cert_path).read_bytes()
    cert = (x509.load_pem_x509_certificate(data) if data.startswith(b"-----BEGIN")
            else x509.load_der_x509_certificate(data))
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return None
    uris = san.value.get_values_for_type(x509.UniformResourceIdentifier)
    return uris[0] if uris else None


async def trust_validator(trusted_dir: Path):  # type: ignore[no-untyped-def]
    """Certificate validator accepting only certificates found in ``trusted_dir``."""
    from asyncua.crypto.truststore import TrustStore
    from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions

    trusted_dir.mkdir(parents=True, exist_ok=True)
    store = TrustStore([trusted_dir], [])
    await store.load()
    options = (CertificateValidatorOptions.TRUSTED_VALIDATION
               | CertificateValidatorOptions.TIME_RANGE | CertificateValidatorOptions.URI)
    return CertificateValidator(options, store)
