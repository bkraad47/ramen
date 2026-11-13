use ramen_node::config::Config;
use ramen_node::log::emit;
use serde_json::json;

#[tokio::main]
async fn main() {
    let cfg = match Config::load() {
        Ok(c) => c,
        Err(e) => {
            emit("error", "bad config", json!({"error": e}));
            std::process::exit(2);
        }
    };
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", cfg.port))
        .await
        .expect("bind");
    let shutdown = async {
        stop_signal().await;
        emit("info", "shutdown", json!({}));
    };
    ramen_node::server::run(cfg, listener, shutdown).await;
}

/// SIGINT stops at once; SIGTERM — what Kubernetes sends a pod — after the drain. Open streams then get their
/// closing result (§23.6).
#[cfg(unix)]
async fn stop_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {
                // keep serving while the load balancer takes this pod out of rotation (see `server::stop_drain`)
                let drain = ramen_node::server::stop_drain(
                    std::env::var("RAMEN_STOP_DRAIN_SECS").ok().as_deref(),
                    std::env::var_os("KUBERNETES_SERVICE_HOST").is_some(),
                );
                if !drain.is_zero() {
                    emit("info", "draining", json!({"secs": drain.as_secs()}));
                    tokio::time::sleep(drain).await;
                }
            }
        },
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

#[cfg(not(unix))]
async fn stop_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
