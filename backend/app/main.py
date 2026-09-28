"""ASGI entry point: `uvicorn app.main:app --reload`"""
from app.api.routes import create_app
from app.config import load_settings

app = create_app(load_settings())
