# Webhook TLS certificate

`proxy-webhooks` (profile `push-webhooks`, ADR 0032) serves HTTPS with the certificate and key
in this directory, for the host name your webhook providers will call:

- `tls.crt`: the certificate, with its chain;
- `tls.key`: its private key, readable by uid 10001 only (`chown 10001:10001 tls.key && chmod 600
  tls.key`).

The stack has no outbound internet by default, so it cannot obtain a certificate by itself;
use your organisation's CA or a certificate issued elsewhere. Both files are ignored by git.
