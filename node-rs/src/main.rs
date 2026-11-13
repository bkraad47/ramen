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

/// SIGINT, or SIGTERM — what Kubernetes sends a pod — so open streams get their closing result (§23.6).
#[cfg(unix)]
async fn stop_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
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
