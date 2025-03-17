# Monitoring Stack for Audio Transcription

This directory contains configuration files for the Prometheus and Grafana monitoring stack, which provides real-time metrics for the audio transcription service.

## Components

1. **Prometheus**: Collects metrics from the application
2. **Grafana**: Provides visualization dashboards for the metrics

## Structure

```
monitoring/
├── prometheus/
│   └── prometheus.yml      # Prometheus configuration
├── grafana/
│   └── provisioning/
│       ├── dashboards/     # Auto-provisioned dashboards
│       │   ├── dashboard.yml
│       │   └── transcription_dashboard.json
│       └── datasources/    # Auto-provisioned data sources
│           └── datasource.yml
└── README.md
```

## Metrics Collected

All metrics are completely anonymous (no user tracking):

- **Total transcriptions processed**
- **File metrics**: sizes, counts, durations
- **Performance metrics**: processing times, queue sizes
- **Error tracking**: counts by error type

## Deployment

The monitoring stack is integrated directly in the main Docker Compose files:

- **Production**: Included in `Docker-compose.yaml` with Traefik integration
- **Local development**: Included in `docker-compose.local.yaml`

## URLs

- **Production**:
  - Application: https://sp000200-t2.kt.ktzh.ch
  - Prometheus: https://sp000200-t6.kt.ktzh.ch
  - Grafana: https://sp000200-t7.kt.ktzh.ch

- **Local development**:
  - Application: http://localhost:8080
  - Prometheus: http://localhost:9090
  - Grafana: http://localhost:3000

## Implementation Details

- Metrics server runs on port 8000
- The application is instrumented using the Prometheus Python client
- Metrics are exposed at `/metrics` endpoint
- Dashboards are auto-provisioned during Grafana startup
