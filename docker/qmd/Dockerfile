# ── Build stage: install npm deps ────────────────────────────────────────────
FROM node:22-alpine AS build
WORKDIR /opt/qmd
RUN echo '{"type":"module","dependencies":{"postgres":"^3"}}' > package.json \
    && npm install --omit=dev

# ── Runtime stage: distroless (no shell, no curl, no apt) ────────────────────
FROM gcr.io/distroless/nodejs22-debian12:nonroot
WORKDIR /opt/qmd

COPY --from=build /opt/qmd/node_modules ./node_modules
COPY --from=build /opt/qmd/package.json ./
COPY server.js embed-index.js entrypoint.js ./

EXPOSE 8686

CMD ["entrypoint.js"]
