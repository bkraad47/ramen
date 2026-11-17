//! Test-only: a throwaway install CA (§39.1) that signs leaves, and a TLS listener for axum stand-ins.
use rcgen::{BasicConstraints, CertificateParams, CertifiedIssuer, IsCa, KeyPair};
use std::path::{Path, PathBuf};

pub struct TestCa {
    issuer: CertifiedIssuer<'static, KeyPair>,
}

impl TestCa {
    pub fn new() -> Self {
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        let issuer = CertifiedIssuer::self_signed(params, KeyPair::generate().unwrap()).unwrap();
        TestCa { issuer }
    }

    pub fn pem(&self) -> String {
        self.issuer.pem()
    }

    /// (cert PEM, key PEM) of a leaf for `names`.
    pub fn leaf(&self, names: &[&str]) -> (String, String) {
        let key = KeyPair::generate().unwrap();
        let params =
            CertificateParams::new(names.iter().map(|n| n.to_string()).collect::<Vec<_>>())
                .unwrap();
        let cert = params.signed_by(&key, &self.issuer).unwrap();
        (cert.pem(), key.serialize_pem())
    }

    /// `ca.crt`, `tls.crt`, `tls.key` in `dir` (the zone Secret's layout); the three paths.
    pub fn write(&self, dir: &Path, names: &[&str]) -> (PathBuf, PathBuf, PathBuf) {
        std::fs::create_dir_all(dir).unwrap();
        let (cert, key) = self.leaf(names);
        let paths = (dir.join("ca.crt"), dir.join("tls.crt"), dir.join("tls.key"));
        std::fs::write(&paths.0, self.pem()).unwrap();
        std::fs::write(&paths.1, cert).unwrap();
        std::fs::write(&paths.2, key).unwrap();
        paths
    }

    /// A rustls acceptor serving a leaf for `localhost` / 127.0.0.1 / `worker.ramen.internal`.
    pub fn acceptor(&self) -> tokio_rustls::TlsAcceptor {
        use rustls_pki_types::pem::PemObject;
        use rustls_pki_types::{CertificateDer, PrivateKeyDer};
        let (cert, key) = self.leaf(&["localhost", "127.0.0.1", "worker.ramen.internal"]);
        let mut config = tokio_rustls::rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(
                vec![CertificateDer::from_pem_slice(cert.as_bytes()).unwrap()],
                PrivateKeyDer::from_pem_slice(key.as_bytes()).unwrap(),
            )
            .unwrap();
        config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
        tokio_rustls::TlsAcceptor::from(std::sync::Arc::new(config))
    }
}

pub fn temp_dir(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
    static N: AtomicU64 = AtomicU64::new(0);
    let d = std::env::temp_dir().join(format!(
        "ramen-{tag}-{}-{}",
        std::process::id(),
        N.fetch_add(1, Relaxed)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// `axum::serve` over TLS: every completed handshake is a connection, a failed one is skipped.
pub struct TlsListener {
    pub inner: tokio::net::TcpListener,
    pub acceptor: tokio_rustls::TlsAcceptor,
}

impl axum::serve::Listener for TlsListener {
    type Io = tokio_rustls::server::TlsStream<tokio::net::TcpStream>;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            let Ok((tcp, addr)) = self.inner.accept().await else {
                continue;
            };
            if let Ok(tls) = self.acceptor.accept(tcp).await {
                return (tls, addr);
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.inner.local_addr()
    }
}
