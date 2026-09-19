# VASP Attribution Engine — Backend

Traces Ethereum funds forward from a suspect address to the centralised exchange
(VASP) where they land, providing the actionable chokepoint for a SAHYOG/I4C
lawful request.

## Quick start

```bash
cp .env.example .env          # fill in ETHERSCAN_API_KEY
uv sync                       # install dependencies
uv run uvicorn main:app --reload --port 8000
```

## Project structure

```
backend/
├── main.py                    # Entry point: uvicorn main:app
├── app/                       # FastAPI application
│   ├── __init__.py            #   App factory + lifespan
│   ├── config.py              #   Central configuration + secrets
│   └── routes.py              #   API endpoints (/trace, /report, /health, /demos)
├── core/                      # Business logic
│   ├── __init__.py
│   ├── tracer.py              #   Forward BFS tracing engine
│   ├── identify.py            #   Exchange identification (4 methods)
│   └── scoring.py             #   Confidence scoring (weighted, explainable)
├── services/                  # Data layer + integrations
│   ├── __init__.py
│   ├── etherscan.py           #   Etherscan API client + in-memory cache
│   ├── graph_store.py         #   Graph storage (Neo4j + NetworkX fallback)
│   ├── replay.py              #   Demo replay / cache
│   └── report.py              #   PDF report generation (reportlab)
└── scripts/                   # CLI tools
    ├── __init__.py
    ├── record_demo.py         #   Record a trace for instant demo replay
    └── import_tagpacks.py     #   Import labels from GraphSense TagPacks
```

## API endpoints

| Endpoint  | Description |
|-----------|-------------|
| `GET /`   | Service info + liveness |
| `GET /health` | Liveness probe, reports graph backend status |
| `GET /trace?address=0x...` | Run or replay a forward trace |
| `GET /report?address=0x...` | Same finding as a downloadable PDF |
| `GET /demos` | List recorded demo traces |

## CLI scripts

```bash
# Record a demo trace for instant replay
uv run python -m scripts.record_demo 0x62425cd6bdcb6bfe51558ea465b063486b70dc9f --depth 3

# List recorded demos
uv run python -m scripts.record_demo --list

# Import labels from GraphSense TagPacks
uv run python -m scripts.import_tagpacks --dry-run
```
