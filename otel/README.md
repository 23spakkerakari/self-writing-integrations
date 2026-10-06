# otel

`collector.yaml` template for the upstream OpenTelemetry Collector Contrib (configuration only,
no custom code): `filelog`, `syslog`, `splunk_hec` and `otlp` receivers, `file_storage`
persistent queue, `otlphttp` exporter to edge-gateway over mTLS. Arrives in M1 (spec 8.1.2).
