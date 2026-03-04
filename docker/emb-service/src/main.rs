use axum::{extract::State, http::StatusCode, routing::{get, post}, Json, Router};
use fastembed::{EmbeddingModel, InitOptions, TextEmbedding};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};

// ── Request / Response types ─────────────────────────────────────────────────

#[derive(Deserialize)]
struct EmbedRequest {
    input: EmbedInput,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum EmbedInput {
    Single(String),
    Batch(Vec<String>),
}

#[derive(Serialize)]
struct EmbedResponse {
    data: Vec<EmbeddingData>,
    model: String,
    usage: Usage,
}

#[derive(Serialize)]
struct EmbeddingData {
    object: &'static str,
    index: usize,
    embedding: Vec<f32>,
}

#[derive(Serialize)]
struct Usage {
    prompt_tokens: usize,
    total_tokens: usize,
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

fn parse_model(name: &str) -> EmbeddingModel {
    match name {
        "bge-small-en-v1.5" => EmbeddingModel::BGESmallENV15,
        "bge-small-en-v1.5-q" => EmbeddingModel::BGESmallENV15Q,
        "bge-base-en-v1.5" => EmbeddingModel::BGEBaseENV15,
        "bge-base-en-v1.5-q" => EmbeddingModel::BGEBaseENV15Q,
        "bge-large-en-v1.5" => EmbeddingModel::BGELargeENV15,
        "bge-large-en-v1.5-q" => EmbeddingModel::BGELargeENV15Q,
        "all-minilm-l6-v2" => EmbeddingModel::AllMiniLML6V2,
        "all-minilm-l6-v2-q" => EmbeddingModel::AllMiniLML6V2Q,
        "nomic-embed-text-v1.5" => EmbeddingModel::NomicEmbedTextV15,
        "nomic-embed-text-v1.5-q" => EmbeddingModel::NomicEmbedTextV15Q,
        _ => {
            eprintln!("Unknown model '{}', falling back to bge-small-en-v1.5-q", name);
            EmbeddingModel::BGESmallENV15Q
        }
    }
}

// ── Shared state ─────────────────────────────────────────────────────────────

struct AppState {
    model: Mutex<TextEmbedding>,
    model_name: String,
}

// ── Handlers ─────────────────────────────────────────────────────────────────

async fn health(State(state): State<Arc<AppState>>) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        model: state.model_name.clone(),
    })
}

async fn embed(
    State(state): State<Arc<AppState>>,
    Json(req): Json<EmbedRequest>,
) -> Result<Json<EmbedResponse>, (StatusCode, Json<ErrorResponse>)> {
    let texts: Vec<String> = match req.input {
        EmbedInput::Single(s) => vec![s],
        EmbedInput::Batch(v) => v,
    };

    if texts.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(ErrorResponse { error: "input must not be empty".into() }),
        ));
    }

    let token_estimate: usize = texts.iter().map(|t| t.split_whitespace().count()).sum();

    // Run inference on blocking threadpool to avoid stalling the tokio runtime.
    let state_clone = Arc::clone(&state);
    let embeddings = tokio::task::spawn_blocking(move || {
        let mut model = state_clone.model.lock().unwrap();
        model.embed(texts, None).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(ErrorResponse { error: e.to_string() })))?
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(ErrorResponse { error: e })))?;

    let data: Vec<EmbeddingData> = embeddings
        .into_iter()
        .enumerate()
        .map(|(i, emb)| EmbeddingData {
            object: "embedding",
            index: i,
            embedding: emb,
        })
        .collect();

    Ok(Json(EmbedResponse {
        data,
        model: state.model_name.clone(),
        usage: Usage {
            prompt_tokens: token_estimate,
            total_tokens: token_estimate,
        },
    }))
}

// ── Main ─────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    let model_name = std::env::var("EMBEDDING_MODEL").unwrap_or_else(|_| "bge-small-en-v1.5-q".into());
    let port = std::env::var("PORT").unwrap_or_else(|_| "3006".into());

    println!("Loading embedding model: {}", model_name);
    let embedding_model = parse_model(&model_name);

    let model = TextEmbedding::try_new(
        InitOptions::new(embedding_model).with_show_download_progress(true),
    )
    .expect("Failed to load embedding model");

    println!("Model loaded successfully");

    let state = Arc::new(AppState {
        model: Mutex::new(model),
        model_name: model_name.clone(),
    });

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/embeddings", post(embed))
        .with_state(state);

    let addr = format!("0.0.0.0:{}", port);
    println!("emb-service listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
