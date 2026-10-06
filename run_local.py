"""Instala las dependencias y ejecuta la copia local en la interfaz de loopback."""

import os
from pathlib import Path
import subprocess
import sys
import venv


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    os.chdir(root)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.create(root / ".venv", with_pip=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements.txt"], check=True)
    subprocess.run([str(python), "-m", "flask", "--app", "app", "run", "--host", "127.0.0.1", "--port", "5028", "--no-debugger", "--no-reload"], check=True)
