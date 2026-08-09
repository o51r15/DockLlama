# API Reference

Base URL: `http://<host>:8556/api`

## Core

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/config` | Current configuration |
| GET | `/containers` | List monitored containers with status |

## Evaluations

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/containers/{name}/evaluate` | Trigger manual evaluation |
| GET | `/containers/{name}/eval-history` | Evaluation history |
| POST | `/containers/{name}/log-evaluate` | Deep log evaluation |
| GET | `/containers/{name}/log-evaluations` | Log eval history |

## Knowledge Base

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/containers/{name}/knowledge` | List knowledge entries |
| POST | `/containers/{name}/knowledge` | Add entry |
| PUT | `/containers/{name}/knowledge/{id}` | Update entry |
| DELETE | `/containers/{name}/knowledge/{id}` | Delete entry |

## Pattern Detection

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/patterns/pending` | Surfaced pending patterns |
| GET | `/patterns/pending/counts` | Badge notification counts |
| GET | `/patterns/pending/all` | All pending patterns |
| POST | `/patterns/pending/{id}/classify` | Classify pattern |
| DELETE | `/patterns/pending/{id}` | Dismiss pattern |

## Trend Analysis

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/containers/{name}/trend-analysis` | Container trends |
| POST | `/containers/{name}/trend-analysis` | Run on-demand analysis |
| GET | `/trend-analysis/fleet` | Fleet-wide trends |

## Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/PUT | `/settings/digest` | Digest configuration |
| GET/PUT | `/settings/retention` | Retention settings |
| GET/PUT | `/models/default` | Default model |
| GET | `/models` | Available Ollama models |
| POST | `/models/test` | Validate a model |

## Container Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/docker/containers` | All Docker containers |
| POST | `/containers/add` | Add container to monitoring |
| DELETE | `/containers/{name}` | Remove from monitoring |
| GET | `/containers/{name}/stats` | Resource stats history |
| GET | `/stats/fleet` | Fleet resource overview |

## Digest

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/digest` | Trigger manual digest |
| GET | `/digests/latest` | Latest digest |
| GET | `/digests/{date}` | Digest by date |
| GET | `/digests` | List all digests |

## Health Checks

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/PUT | `/containers/{name}/healthcheck` | Configure HTTP health check |
| DELETE | `/containers/{name}/healthcheck` | Remove health check |
| GET | `/healthchecks` | List all health checks |

## Blackout Windows

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/blackouts` | List blackout windows |
| POST | `/blackouts` | Create blackout window |
| PUT/DELETE | `/blackouts/{id}` | Update/delete window |
