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
        let _ = tokio::signal::ctrl_c().await;
        emit("info", "shutdown", json!({}));
    };
    ramen_node::server::run(cfg, listener, shutdown).await;
}
