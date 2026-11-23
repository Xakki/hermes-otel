---
sidebar_position: 9
title: "Graylog + Prometheus via Collector"
description: "Send Hermes API logs to Graylog and account-labelled metrics to Prometheus through one existing OTel Collector."
---

# Graylog + Prometheus via Collector

The plugin exports **OTLP/HTTP** to an OpenTelemetry Collector. The Collector forwards the logs to Graylog over **OTLP/gRPC** and the metrics to Prometheus through **Remote Write**. The plugin does not send GELF or connect directly to Graylog/Prometheus. This arrangement needs an OTLP HTTP receiver and two separate signal pipelines in the Collector; a reachable port alone does not prove ingestion.

```text
Hermes + hermes-otel --OTLP/HTTP--> Collector:4318
                                     | logs    --OTLP/gRPC--> Graylog
                                     | metrics --Remote Write--> Prometheus --> Grafana
```

## Plugin configuration

Put the settings in `$HERMES_HOME/hermes_otel.yaml` (not Hermes' main `config.yaml`). Replace `COLLECTOR_HOST` with an address reachable from the Hermes process:

```yaml
enabled: true
level: info                # at most request/response previews
logs:
  events:
    enabled: true
    content: preview
dashboard_live: false      # optional: do not retain a local copy of telemetry
backends:
  - type: otlp
    name: request-observability
    endpoint: http://COLLECTOR_HOST:4318/v1/traces
    traces: false           # this route is for logs and metrics, not trace storage
    metrics: true
    logs: true
```

The OTLP backend derives `/v1/metrics` and `/v1/logs` from the `/v1/traces` URL. `traces: false` prevents span export but does not disable logs or metrics. A Collector that exports traces only to its `debug` exporter would otherwise write captured prompts to Collector logs.

Metrics are enabled by default; `metrics.enabled: false` disables the metrics route without disabling events. `level: debug` permits redacted message content only where the capture policy allows it; it does not include raw request options or transport headers. `level: info` permits previews, and `level: notice` keeps events but omits their content. Python logging is configured independently. See [Logs and events](/configuration/logs#provider-request-events) for per-backend narrowing and account-label privacy. If you want a local copy, leave `dashboard_live` enabled (the default).

## Collector prerequisites

Check the existing Collector rather than starting a second listener:

- `receivers.otlp.protocols.http` must accept the plugin's OTLP/HTTP requests.
- `service.pipelines.logs` must route from `otlp` to an `otlp` exporter targeting a Graylog OTLP/gRPC input.
- `service.pipelines.metrics` must route from `otlp` to `prometheusremotewrite`.
- Prometheus must enable its Remote Write receiver. Metrics are not delivered by a scrape of the plugin process.

Do not infer a working route from TCP connectivity. A Collector `debug` exporter is useful for diagnostics but is not Graylog or Prometheus persistence. When enabling `debug`, account for the content that any Collector debug exporter could write to its own logs.

## Manual verification

After configuring the route, send **synthetic** non-secret API request/response events and a metric. Search Graylog for the unique `api_request_id` and inspect `provider`, `model`, and the pool-entry labels where available. Verify that the `hermes.api.request` and `hermes.api.response` events carry no more content than configured. Query Prometheus for `hermes_api_request_count_total` and `hermes_token_usage_total` with the same non-secret account labels. An exporter success response is not a substitute for reading each destination back.

For a starting visualization, [download the portable Grafana dashboard](/dashboards/hermes-request-observability.json) and import its JSON manually, selecting your Prometheus datasource. A [provisioning snapshot with expanded metric coverage](/dashboards/hermes-request-observability-provisioned.json) is also available, but it binds panels to one deployment's Prometheus datasource UID; replace that UID for your own installation. The snapshots share a dashboard UID, so do not import both into the same Grafana organization. Their PromQL panels use aggregate metrics; use Graylog's `api_request_id` for per-request investigation. Neither JSON file is evidence that your live pipelines have ingested data.
