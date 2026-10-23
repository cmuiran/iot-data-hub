"""A private CA with server and client certificates, for TLS tests (no network, no openssl)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass
class Pair:
    cert: Path
    key: Path


def _write(directory: Path, name: str, cert: x509.Certificate,
           key: ec.EllipticCurvePrivateKey) -> Pair:
    cert_path = directory / f"{name}.crt.pem"
    key_path = directory / f"{name}.key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return Pair(cert_path, key_path)


class PlantCA:
    def __init__(self, directory: Path, name: str = "Test Plant CA") -> None:
        self.directory = directory
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.cert = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(self.key, hashes.SHA256()))
        self.pair = _write(directory, name.lower().replace(" ", "-"), self.cert, self.key)

    def issue(self, name: str, *, server_names: list[str] | None = None) -> Pair:
        """A server certificate (with ``server_names``) or else a client certificate."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self.cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True))
        if server_names:
            alt: list[x509.GeneralName] = []
            for n in server_names:
                try:
                    alt.append(x509.IPAddress(ipaddress.ip_address(n)))
                except ValueError:
                    alt.append(x509.DNSName(n))
            builder = builder.add_extension(x509.SubjectAlternativeName(alt), critical=False)
            usage = ExtendedKeyUsageOID.SERVER_AUTH
        else:
            usage = ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        return _write(self.directory, name, builder.sign(self.key, hashes.SHA256()), key)
