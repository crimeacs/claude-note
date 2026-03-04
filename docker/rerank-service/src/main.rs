use axum::{extract::State, http::StatusCode, routing::{get, post}, Json, Router};
use fastembed::{RerankInitOptions, RerankerModel, TextRerank};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};

// ── Request / Response types (Cohere-compatible) ─────────────────────────────

#[derive(Deserialize)]
struct RerankRequest {
    query: String,
    documents: Vec<String>,
    top_n: Option<usize>,
}

#[derive(Serialize)]
struct RerankResponse {
    results: Vec<RerankResultItem>,
    model: String,
}

#[derive(Serialize)]
struct RerankResultItem {
    index: usize,
    relevance_score: f32,
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    model: String,
}

#[derive(Serialize)]
struct ErrorResponse {
    error: String,
}

// ── Model mapping ────────────────────────────────────────────────────────────

fn parse_model(name: &str) -> RerankerModel {
    match name {
        "bge-reranker-base" => RerankerModel::BGERerankerBase,
        "bge-reranker-v2-m3" => RerankerModel::BGERerankerV2M3,
        "jina-reranker-v1-turbo-en" => RerankerModel::JINARerankerV1TurboEn,
        "jina-reranker-v2-base-multilingual" => RerankerModel::JINARerankerV2BaseMultiligual,
        _ => {
            eprintln!("Unknown model '{}', falling back to jina-reranker-v1-turbo-en", name);
            RerankerModel::JINARerankerV1TurboEn
        }
    }
}

// ── Shared state ─────────────────────────────────────────────────────────────

struct AppState {
    model: Mutex<TextRerank>,
    model_name: String,
}

// ── Handlers ─────────────────────────────────────────────────────────────────

async fn health(State(state): State<Arc<AppState>>) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        model: state.model_name.clone(),
    })
}

async fn rerank(
    State(state): State<Arc<AppState>>,
    Json(req): Json<RerankRequest>,
) -> Result<Json<RerankResponse>, (StatusCode, Json<ErrorResponse>)> {
    if req.documents.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(ErrorResponse { error: "documents must not be empty".into() }),
        ));
    }

    // Run inference on blocking threadpool to avoid stalling the tokio runtime.
    // std::sync::Mutex held across a CPU-bound ONNX call would block the executor.
    let state_clone = Arc::clone(&state);
    let items = tokio::task::spawn_blocking(move || {
        let doc_refs: Vec<&str> = req.documents.iter().map(String::as_str).collect();
        let mut model = state_clone.model.lock().unwrap();
        // fastembed's 4th param is batch_size, NOT top_n.
        // Pass None to use default batch_size (all docs in one ONNX call).
        // Results are already sorted by score desc; we truncate to top_n after.
        let results = model.rerank(req.query.as_str(), doc_refs.as_slice(), false, None)
            .map_err(|e| e.to_string())?;
        let items: Vec<RerankResultItem> = results.into_iter().map(|r| RerankResultItem {
            index: r.index,
            relevance_score: r.score,
        }).collect();
        Ok::<Vec<RerankResultItem>, String>(match req.top_n {
            Some(n) => items.into_iter().take(n).collect(),
            None => items,
        })
    })
    .await
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(ErrorResponse { error: e.to_string() })))?
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(ErrorResponse { error: e })))?;

    Ok(Json(RerankResponse {
        results: items,
        model: state.model_name.clone(),
    }))
}

// ── Main ─────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    let model_name = std::env::var("RERANK_MODEL").unwrap_or_else(|_| "jina-reranker-v1-turbo-en".into());
    let port = std::env::var("PORT").unwrap_or_else(|_| "3007".into());

    println!("Loading rerank model: {}", model_name);
    let reranker_model = parse_model(&model_name);

    let model = TextRerank::try_new(
        RerankInitOptions::new(reranker_model).with_show_download_progress(true),
    )
    .expect("Failed to load rerank model");

    println!("Model loaded successfully");

    let state = Arc::new(AppState {
        model: Mutex::new(model),
        model_name: model_name.clone(),
    });

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/rerank", post(rerank))
        .with_state(state);

    let addr = format!("0.0.0.0:{}", port);
    println!("rerank-service listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
