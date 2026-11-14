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
    ramen_node::log::set_activator(cfg.activator); // §24.1: every line of an activator says so
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
///
/// §24.1: an activator takes the same path on purpose. It sits behind the zone's `worker` Service, so when the
/// console scales it to 0 after a wake the edge keeps routing to it for the same while as to a stopping worker;
/// it goes on forwarding through the drain, and forwarded calls still in flight get the same `STOP_GRACE`.
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
