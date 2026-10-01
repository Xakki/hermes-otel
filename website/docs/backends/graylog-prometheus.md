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
level: info                # request/response previews, INFO+ Hermes Python logs
metrics:
  enabled: true            # explicit global opt-in; a metrics-capable backend is not enough
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

`level: debug` includes sanitized model-facing request options, messages, and response bodies, capped at 128,000 characters per event with truncation metadata. Large bodies are split into UTF-8-safe parts of at most 8,192 bytes, each with the same `api_request_id` and `body_part_index` / `body_part_count` (1-based), so Graylog retains them; concatenate the body field in part order to reconstruct it. The non-message options come from Hermes's bounded `request_options` hook field; if the host lacks it or its own cap is exceeded, `request_truncated` is true. `level: info` sends short previews with original length and a truncation flag. `level: notice` sends only Hermes Python WARNING/ERROR/CRITICAL records, **not** request/response events; account attribution remains available in metrics when enabled. See [OTel logs](/configuration/logs). The selected credential-pool entry contributes `credential_id` and the unmodified account label `account_name` to request events and API metrics when available. No credential token is used as an identity label.

## Collector prerequisites

Check the existing Collector rather than starting a second listener:

- `receivers.otlp.protocols.http` must accept the plugin's OTLP/HTTP requests.
- `service.pipelines.logs` must route from `otlp` to an `otlp` exporter targeting a Graylog OTLP/gRPC input.
- `service.pipelines.metrics` must route from `otlp` to `prometheusremotewrite`.
- Prometheus must enable its Remote Write receiver. Metrics are not delivered by a scrape of the plugin process.

Do not infer a working route from TCP connectivity. A Collector `debug` exporter is useful for diagnostics but is not Graylog or Prometheus persistence. When enabling `debug`, account for the content that any Collector debug exporter could write to its own logs.

## Manual verification

After configuring the route, send **synthetic** non-secret API request/response events and a metric. Search Graylog for the unique `api_request_id` and inspect `provider`, `model`, `credential_id`, `account_name`, severity, and mode-specific body/truncation fields. Query Prometheus for `hermes_api_request_count_total` and `hermes_token_usage_total` with the same non-secret account labels. An exporter success response is not a substitute for reading each destination back.

For a starting visualization, [download the portable Grafana dashboard](/dashboards/hermes-request-observability.json) and import its JSON manually, selecting your Prometheus datasource. A [provisioning snapshot with expanded metric coverage](/dashboards/hermes-request-observability-provisioned.json) is also available, but it binds panels to one deployment's Prometheus datasource UID; replace that UID for your own installation. The snapshots share a dashboard UID, so do not import both into the same Grafana organization. Their PromQL panels use aggregate metrics; use Graylog's `api_request_id` for per-request investigation. Neither JSON file is evidence that your live pipelines have ingested data.
