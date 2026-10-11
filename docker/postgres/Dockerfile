# Development-only TLS database. Production uses its managed TLS database.
FROM postgres:14
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
COPY start-tls.sh /usr/local/bin/start-tls.sh
ENTRYPOINT ["sh", "/usr/local/bin/start-tls.sh"]
CMD ["postgres"]
