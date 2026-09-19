"""
FastAPI application factory and lifespan management.

WHY a factory rather than a module-level `app` object: the routes import from
`core` and `services`, and those import `app.config`. A factory delays the
route wiring until after all packages are importable, breaking the circular
dependency that a flat import would create.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.etherscan import get_client
from services.graph_store import reset_driver


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Release the shared httpx pool and the Neo4j driver when the server stops."""
    yield
    await get_client().aclose()
    reset_driver()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        lifespan=lifespan,
        title="Crypto Wallet -> VASP Attribution Engine",
        description=(
            "Traces Ethereum funds forward from a suspect address to the "
            "centralised exchange where they land — the actionable chokepoint "
            "for a SAHYOG/I4C lawful request."
        ),
        version="0.1.0",
    )

    # The React + Vite frontend runs on a different origin during development,
    # so the browser needs explicit permission to call us.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.routes import router
    application.include_router(router)

    return application
