# ── Perlite (Obsidian vault viewer) — non-root, minimal Alpine ────────────────
FROM sec77/perlite:latest AS perlite

FROM alpine:3.21

RUN apk add --no-cache \
    php84-cli php84-mbstring php84-ctype php84-iconv \
    php84-session php84-fileinfo php84-openssl \
    && ln -s /usr/bin/php84 /usr/bin/php \
    && adduser -D -u 65532 nonroot

COPY --from=perlite --chown=65532:65532 /var/www/perlite /var/www/perlite

USER 65532
WORKDIR /var/www/perlite

EXPOSE 8080

CMD ["php", "-S", "0.0.0.0:8080", "-t", "/var/www/perlite"]
