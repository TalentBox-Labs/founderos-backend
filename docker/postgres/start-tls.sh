#!/bin/sh
set -eu
# Generate a fresh local certificate outside PGDATA on each container start.
install -d -m 700 -o postgres -g postgres /tmp/founderos-postgres-tls
openssl req -new -x509 -nodes -days 30 \
  -subj '/CN=db' \
  -keyout /tmp/founderos-postgres-tls/server.key \
  -out /tmp/founderos-postgres-tls/server.crt >/dev/null 2>&1
chown postgres:postgres /tmp/founderos-postgres-tls/server.key /tmp/founderos-postgres-tls/server.crt
chmod 600 /tmp/founderos-postgres-tls/server.key
exec docker-entrypoint.sh "$@" \
  -c ssl=on \
  -c ssl_cert_file=/tmp/founderos-postgres-tls/server.crt \
  -c ssl_key_file=/tmp/founderos-postgres-tls/server.key
