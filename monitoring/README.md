# Monitoring and search configuration

This directory contains the optional observability stack's configuration and the SearxNG configuration used by the main application.

## Contents

| Path | Purpose |
| --- | --- |
| `victoria-metrics/prometheus.yml` | Prometheus-compatible scrape configuration |
| `grafana/provisioning/datasources/datasource.yml` | Provision VictoriaMetrics as Grafana's default data source |
| `grafana/provisioning/dashboards/` | Dashboard provider plus system-health and AI-performance dashboards |
| `searxng-settings.yml` | Search engine configuration mounted by the base Compose stack |
| `searxng-limiter.toml` | SearxNG limiter configuration |

SearxNG is part of the base app, not the optional monitoring stack.

## Start and stop

From the repository root:

```sh
make monitor
make monitor-down
```

`make monitor` starts the production Compose stack with [docker-compose.monitoring.yml](../docker-compose.monitoring.yml). It is not the Vite development stack. The monitoring-only stop target leaves application services running.

| Service | Local address |
| --- | --- |
| Grafana | http://localhost:3000 |
| VictoriaMetrics | http://localhost:8428 |
| Node Exporter | http://localhost:9100 |

The scrape interval is **15 seconds**. VictoriaMetrics collects backend `/metrics`, Node Exporter metrics, and its own metrics. Grafana queries VictoriaMetrics over the Compose network. Retention is configured to **30 days**.

## Persistence and caveats

Metrics and Grafana state use named volumes `vm-data` and `grafana-data`. Removing those volumes removes stored telemetry and dashboard state.

Node Exporter mounts host `/proc`, `/sys`, and the root filesystem read-only. Under Docker Desktop, metrics can reflect the Docker Linux VM rather than the physical macOS/Windows host.

The shipped Grafana configuration enables anonymous access and defaults the admin password to `admin` unless `GRAFANA_ADMIN_PASSWORD` is set. Treat all three published monitoring ports as local-only; disable anonymous access, change credentials, and restrict network access before remote exposure.
