from cryptography import x509

from ramen_console.tls import self_signed


def test_self_signed(tmp_path):
    key, cert = self_signed(tmp_path / "tls", "console.local")
    assert key.exists() and cert.exists()
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    assert "console.local" in str(c.subject)
