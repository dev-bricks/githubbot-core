# Git client certificates in bot transport

The bot transport uses one process-local bot token, an isolated AskPass helper,
exact origin/ref checks and verified TLS. System/global Git configuration and
certificate environment overrides are excluded from that process.

Empty `http.sslCert=` / `http.sslKey=` path overrides can make Git reject even
an unauthenticated public read with `credential missing host field`. The
transport therefore rejects local client-certificate configuration (including
URL scopes and includes) before push instead of overriding those paths with
empty strings. It reads key names only and fails closed on probe errors.

All other credential/header/cookie overrides, HTTPS verification, identity
checks and exact remote readback remain in place. A host configured for a TLS
client certificate needs a separate reviewed deployment policy; this change
does not activate certificate authentication or alter host configuration.
