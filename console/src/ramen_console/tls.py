"""Generate a self-signed certificate: python -m ramen_console.tls <dir> [hostname]."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def self_signed(out: Path, hostname: str = "localhost") -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    key_path, cert_path = out / "key.pem", out / "cert.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


if __name__ == "__main__":  # pragma: no cover
    k, c = self_signed(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "localhost")
    print(k, c)
