# ── Build stage (Ubuntu 24.04 = glibc 2.39, required by ort/ONNX Runtime) ────
FROM ubuntu:24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential pkg-config libssl-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain 1.88.0
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /build
COPY Cargo.toml ./
COPY src/ src/

RUN cargo build --release \
    && strip target/release/rerank-service \
    && strip target/release/healthcheck

# Collect all runtime shared libs into /runtime
RUN mkdir /runtime && \
    ldd target/release/rerank-service | awk '/=>/ {print $3}' | sort -u | \
    while read f; do [ -f "$f" ] && cp "$f" /runtime/; done && \
    cp /usr/lib/*/libgomp.so.1 /runtime/ 2>/dev/null; true

# Copy the dynamic linker (arch-agnostic)
RUN cp /lib/ld-linux-*.so.* /runtime/ 2>/dev/null; true

# Create minimal /etc/passwd for nonroot user (uid 65532)
RUN echo 'root:x:0:0::/root:/sbin/nologin' > /runtime/passwd && \
    echo 'nonroot:x:65532:65532::/home/nonroot:/sbin/nologin' >> /runtime/passwd && \
    echo 'root:x:0:' > /runtime/group && \
    echo 'nonroot:x:65532:' >> /runtime/group

# ── Minimal runtime (scratch + libs only) ────────────────────────────────────
FROM scratch

# Shared libraries + dynamic linker
COPY --from=builder /runtime/ld-linux-*.so.* /lib/
COPY --from=builder /runtime/lib*.so.* /lib/

# TLS certificates
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# User setup
COPY --from=builder /runtime/passwd /etc/passwd
COPY --from=builder /runtime/group /etc/group

# Binaries
COPY --from=builder /build/target/release/rerank-service /usr/local/bin/rerank-service
COPY --from=builder /build/target/release/healthcheck /usr/local/bin/healthcheck

USER 65532:65532
WORKDIR /home/nonroot

ENV PORT=3007
ENV RERANK_MODEL=jina-reranker-v1-turbo-en

EXPOSE 3007

ENTRYPOINT ["rerank-service"]
