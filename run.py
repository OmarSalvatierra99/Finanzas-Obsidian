#!/usr/bin/env python3
"""Punto de entrada local de Finanzas."""

from app import app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=app.config["PORT"], debug=False)
