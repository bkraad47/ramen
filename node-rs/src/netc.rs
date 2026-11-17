//! The node's own outgoing HTTP (§24): the activator's wake request and the OTLP posts. One HTTP/1.1 request per
//! connection over hyper's connection API — a few requests a minute at most, so no pool — with `https://` through
//! the rustls the node already serves with, verified against the bundled web roots and (§39.2) `RAMEN_TLS_CA`.
use http::header::{CONTENT_TYPE, HOST};
use http::{HeaderName, HeaderValue, Method, Request, Uri};
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Bytes;
use hyper_util::rt::TokioIo;
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::net::TcpStream;

/// What is read of an answer's body before the rest is left unread (a collector's error text, a wake reply).
const MAX_ANSWER_BYTES: usize = 64 << 10;

/// The public web roots, plus the PEM bundle at `ca` when given (§39.2 `RAMEN_TLS_CA`: the install CA). Built once
/// per bundle content, so a rotated bundle is picked up by the next request.
pub fn client_config(ca: Option<&Path>) -> Result<Arc<tokio_rustls::rustls::ClientConfig>, String> {
    use rustls_pki_types::CertificateDer;
    use rustls_pki_types::pem::PemObject;
    type Cache = Mutex<Vec<(Vec<u8>, Arc<tokio_rustls::rustls::ClientConfig>)>>;
    static CACHE: OnceLock<Cache> = OnceLock::new();
    let pem = match ca {
        Some(p) => std::fs::read(p).map_err(|e| format!("RAMEN_TLS_CA {}: {e}", p.display()))?,
        None => Vec::new(),
    };
    let cache = CACHE.get_or_init(Default::default);
    let mut cache = cache.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((_, c)) = cache.iter().find(|(k, _)| *k == pem) {
        return Ok(c.clone());
    }
    let mut roots = tokio_rustls::rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    for cert in CertificateDer::pem_slice_iter(&pem) {
        let cert = cert.map_err(|_| "RAMEN_TLS_CA: not a PEM bundle".to_string())?;
        roots.add(cert).map_err(|e| format!("RAMEN_TLS_CA: {e}"))?;
    }
    let config = Arc::new(
        tokio_rustls::rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth(),
    );
    if cache.len() >= 4 {
        cache.remove(0);
    }
    cache.push((pem, config.clone()));
    Ok(config)
}

/// `POST url` with a JSON body; the answer's status and the start of its body. An error is a short text that
/// never quotes a header value or the body.
pub async fn post_json(
    url: &str,
    headers: &[(String, String)],
    body: Vec<u8>,
    timeout: Duration,
    ca: Option<&Path>,
) -> Result<(u16, Bytes), String> {
    tokio::time::timeout(timeout, post(url, headers, body, ca))
        .await
        .map_err(|_| format!("no answer within {}s", timeout.as_secs()))?
}

async fn post(
    url: &str,
    headers: &[(String, String)],
    body: Vec<u8>,
    ca: Option<&Path>,
) -> Result<(u16, Bytes), String> {
    let uri: Uri = url.parse().map_err(|_| "not a URL".to_string())?;
    let https = match uri.scheme_str() {
        Some("http") => false,
        Some("https") => true,
        _ => return Err("the URL must be http:// or https://".into()),
    };
    let host = uri.host().ok_or("the URL has no host")?.to_string();
    let port = uri.port_u16().unwrap_or(if https { 443 } else { 80 });
    let mut req = Request::builder()
        .method(Method::POST)
        .uri(uri.path_and_query().map_or("/", |p| p.as_str()))
        .header(HOST, uri.authority().map_or(host.as_str(), |a| a.as_str()))
        .header(CONTENT_TYPE, "application/json")
        .body(Full::new(Bytes::from(body)))
        .map_err(|e| e.to_string())?;
    for (k, v) in headers {
        if let (Ok(k), Ok(v)) = (
            HeaderName::from_bytes(k.as_bytes()),
            HeaderValue::from_str(v),
        ) {
            req.headers_mut().insert(k, v);
        }
    }
    let tcp = TcpStream::connect((host.trim_matches(['[', ']']), port))
        .await
        .map_err(|e| format!("connect: {e}"))?;
    let resp = if https {
        let name =
            rustls_pki_types::ServerName::try_from(host.trim_matches(['[', ']']).to_string())
                .map_err(|_| "bad host name".to_string())?;
        let stream = tokio_rustls::TlsConnector::from(client_config(ca)?)
            .connect(name, tcp)
            .await
            .map_err(|e| format!("tls: {e}"))?;
        send(TokioIo::new(stream), req).await?
    } else {
        send(TokioIo::new(tcp), req).await?
    };
    let status = resp.status().as_u16();
    let body = Limited::new(resp.into_body(), MAX_ANSWER_BYTES)
        .collect()
        .await
        .map(|b| b.to_bytes())
        .unwrap_or_default();
    Ok((status, body))
}

async fn send<I>(
    io: I,
    req: Request<Full<Bytes>>,
) -> Result<hyper::Response<hyper::body::Incoming>, String>
where
    I: hyper::rt::Read + hyper::rt::Write + Unpin + Send + 'static,
{
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|e| format!("handshake: {e}"))?;
    // the connection task ends with the response body (one request per connection)
    tokio::spawn(conn);
    sender
        .send_request(req)
        .await
        .map_err(|e| format!("request: {e}"))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use axum::extract::State;
    use axum::http::HeaderMap;
    use std::net::SocketAddr;
    use std::sync::Mutex;

    /// What a stand-in server (console, collector) was sent: path, headers, body.
    pub type Seen = Arc<Mutex<Vec<(String, HeaderMap, String)>>>;

    /// An HTTP server that records every POST and answers `status`.
    pub async fn recorder(status: u16) -> (SocketAddr, Seen) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (app, seen) = recording(status);
        tokio::spawn(async move { axum::serve(listener, app).await });
        (addr, seen)
    }

    /// The same over TLS, with a leaf of `ca` (`localhost`, 127.0.0.1, `worker.ramen.internal`).
    pub async fn tls_recorder(ca: &crate::testca::TestCa, status: u16) -> (SocketAddr, Seen) {
        let inner = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = inner.local_addr().unwrap();
        let (app, seen) = recording(status);
        let listener = crate::testca::TlsListener {
            inner,
            acceptor: ca.acceptor(),
        };
        tokio::spawn(async move { axum::serve(listener, app).await });
        (addr, seen)
    }

    pub fn recording(status: u16) -> (axum::Router, Seen) {
        let seen: Seen = Default::default();
        let app = axum::Router::new()
            .fallback(
                |State((seen, status)): State<(Seen, u16)>, req: axum::extract::Request| async move {
                    let (parts, body) = req.into_parts();
                    let body = axum::body::to_bytes(body, 4 << 20).await.unwrap_or_default();
                    seen.lock().unwrap().push((
                        parts.uri.path().to_string(),
                        parts.headers,
                        String::from_utf8_lossy(&body).to_string(),
                    ));
                    (
                        axum::http::StatusCode::from_u16(status).unwrap(),
                        "{\"ok\":true}",
                    )
                },
            )
            .with_state((seen.clone(), status));
        (app, seen)
    }

    /// §39.2: `RAMEN_TLS_CA` is trusted besides the public roots — a collector or console signed by the install CA
    /// is reached, the same server without the bundle is refused, and a missing bundle is a short error.
    #[tokio::test]
    async fn https_trusts_the_install_ca_when_given() {
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("netc");
        let bundle = dir.join("ca.crt");
        std::fs::write(&bundle, ca.pem()).unwrap();
        let (addr, seen) = tls_recorder(&ca, 202).await;
        let t = Duration::from_secs(5);
        for host in ["localhost", "127.0.0.1"] {
            let url = format!("https://{host}:{}/v1/traces", addr.port());
            let e = post_json(&url, &[], vec![], t, None).await.unwrap_err();
            assert!(e.starts_with("tls:"), "{e}");
            let (status, _) = post_json(&url, &[], b"{}".to_vec(), t, Some(&bundle))
                .await
                .unwrap();
            assert_eq!(status, 202);
        }
        assert_eq!(seen.lock().unwrap().len(), 2);
        let e = post_json(
            &format!("https://localhost:{}/x", addr.port()),
            &[],
            vec![],
            t,
            Some(&dir.join("nope.crt")),
        )
        .await
        .unwrap_err();
        assert!(e.contains("RAMEN_TLS_CA"), "{e}");
    }

    #[tokio::test]
    async fn posts_json_with_headers_and_reads_the_answer() {
        let (addr, seen) = recorder(202).await;
        let hs = vec![("x-ramen-admin-key".to_string(), "adm".to_string())];
        let (status, body) = post_json(
            &format!("http://{addr}/a/b?c=1"),
            &hs,
            b"{\"k\":1}".to_vec(),
            Duration::from_secs(5),
            None,
        )
        .await
        .unwrap();
        assert_eq!((status, body.as_ref()), (202, &b"{\"ok\":true}"[..]));
        let seen = seen.lock().unwrap();
        let (path, headers, sent) = &seen[0];
        assert_eq!((path.as_str(), sent.as_str()), ("/a/b", "{\"k\":1}"));
        assert_eq!(headers["x-ramen-admin-key"], "adm");
        assert_eq!(headers["content-type"], "application/json");
    }

    #[tokio::test]
    async fn failures_are_short_texts_without_the_credential() {
        let hs = vec![("authorization".to_string(), "Bearer s3cr3t".to_string())];
        let t = Duration::from_secs(2);
        for url in ["ftp://x/y", "not a url", "http://127.0.0.1:1/x"] {
            let e = post_json(url, &hs, vec![], t, None).await.unwrap_err();
            assert!(!e.contains("s3cr3t") && e.len() < 200, "{e}");
        }
        // `https://` is verified TLS: a server that does not speak it is an error, not a silent downgrade
        let (plain, seen) = recorder(200).await;
        let e = post_json(
            &format!("https://localhost:{}/x", plain.port()),
            &hs,
            vec![],
            t,
            None,
        )
        .await
        .unwrap_err();
        assert!(e.starts_with("tls:") && !e.contains("s3cr3t"), "{e}");
        assert!(seen.lock().unwrap().is_empty());
        // a server that accepts and never answers runs into the timeout
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let hold = tokio::spawn(async move {
            let (s, _) = listener.accept().await.unwrap();
            tokio::time::sleep(Duration::from_secs(5)).await;
            drop(s);
        });
        let e = post_json(
            &format!("http://{addr}/"),
            &hs,
            vec![],
            Duration::from_millis(200),
            None,
        )
        .await
        .unwrap_err();
        assert!(e.contains("no answer"), "{e}");
        hold.abort();
    }
}
