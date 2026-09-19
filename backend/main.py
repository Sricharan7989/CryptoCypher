"""
Entry point for the VASP Attribution Engine backend.

Run with:  uvicorn main:app --reload --port 8000   (from backend/)
"""

from app import create_app

app = create_app()
