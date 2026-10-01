---
sidebar_position: 8
title: "Generic OTLP"
description: "Send Hermes traces to any OTLP/HTTP-compatible collector — Honeycomb, New Relic, Datadog's OTLP ingest, your company's central collector, anything."
---

# Generic OTLP

If your backend isn't on the list but it accepts **OTLP over HTTP**, the generic `otlp` type has you covered. Point at an endpoint, set any auth headers, and go.

**Signals:** traces, logs and metrics are individually selectable per backend; metrics additionally require the global `metrics.enabled: true` opt-in.

## Typical use cases

- Company-wide OTel collector (OpenTelemetry Collector, Grafana Alloy, etc.)
- Commercial vendors with an OTLP ingest path (Honeycomb, New Relic, Datadog, Elastic APM, Uptrace, OpenObserve)
- Custom sink you're prototyping

## Configuration

Via `config.yaml`:

```yaml
metrics:
  enabled: true
backends:
  - type: otlp
    name: my-collector           # Friendly name shown in logs
    endpoint: http://collector:4318/v1/traces
    headers:
      X-Auth: ${COLLECTOR_AUTH}
      X-Tenant: acme
    metrics: true                # Backend allows metrics; global opt-in is still required
    logs: true                   # Collector must accept /v1/logs
```

The `name` is purely for log output ("`[hermes-otel] ✓ my-collector connected`") — it doesn't affect the spans themselves.

## Examples

### Honeycomb

```yaml
backends:
  - type: otlp
    name: honeycomb
    endpoint: https://api.honeycomb.io/v1/traces
    headers:
      x-honeycomb-team: ${HONEYCOMB_API_KEY}
      x-honeycomb-dataset: hermes-agent
```

### telemetry.dev

```yaml
backends:
  - type: otlp
    name: telemetry-dev
    endpoint: https://ingest.telemetry.dev/v1/traces
    headers:
      Authorization: Bearer ${TELEMETRY_DEV_API_KEY}
```

See the dedicated [telemetry.dev](/backends/telemetry) page for endpoints, attribute fit, and troubleshooting.

### New Relic (OTLP endpoint)

```yaml
backends:
  - type: otlp
    name: new-relic
    endpoint: https://otlp.nr-data.net/v1/traces
    headers:
      api-key: ${NEW_RELIC_LICENSE_KEY}
```

### Datadog (via Datadog Agent with OTLP ingest)

```yaml
backends:
  - type: otlp
    name: datadog
    endpoint: http://datadog-agent:4318/v1/traces
    # Datadog's OTLP ingest goes through the local agent; no auth header needed there.
```

### A central OTel Collector

```yaml
backends:
  - type: otlp
    name: central
    endpoint: http://otel-collector.internal:4318/v1/traces
```

### Logs and metrics without traces

Set `traces: false` on the backend entry; logs and globally enabled metrics still export. The plugin derives `/v1/metrics` and `/v1/logs` by replacing `/v1/traces`. This fits an existing Collector forwarding OTLP/gRPC logs to Graylog and Remote Write metrics to Prometheus: [full example](/backends/graylog-prometheus). If those derived paths do not match your receiver, the generic backend needs a separate endpoint override.

## Env var auth

Secrets inline in YAML are discouraged. Use env var interpolation (`${VAR_NAME}`) for headers:

```yaml
backends:
  - type: otlp
    endpoint: https://api.honeycomb.io/v1/traces
    headers:
      x-honeycomb-team: ${HONEYCOMB_API_KEY}
```

Alternatively, set global headers once (applies to every backend):

```yaml
headers:
  X-Company-Auth: ${SHARED_SECRET}

backends:
  - type: otlp
    endpoint: https://api.honeycomb.io/v1/traces
    headers:
      x-honeycomb-team: ${HONEYCOMB_API_KEY}
```

Per-backend headers are merged on top of global headers; same-named keys get overwritten by the per-backend value.

## Troubleshooting

**"Which attribute convention does my backend use?"**

The plugin emits **both** `gen_ai.*` and OpenInference `llm.*` on every span — whichever one your backend indexes on, it'll pick up.

**"Traces show up; metrics don't"**

- Set `metrics.enabled: true` globally in the plugin YAML; `metrics: true` in a backend entry alone does not turn metrics on.
- Not every backend accepts OTLP metrics. Set `metrics: false` on the backend entry to silence the metrics exporter.
- If your metrics endpoint isn't `/v1/metrics` derived from the trace endpoint, open an issue.

**"TLS handshake errors"**

- The plugin uses `opentelemetry-exporter-otlp-proto-http`, which relies on the system cert store. If you're behind a corporate MITM CA, you may need `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` pointing at your CA bundle.
