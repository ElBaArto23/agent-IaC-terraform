"""Configuración compartida: lee el .env y las variables de entorno en un solo lugar.

AGENT_OUTPUT_DIR  carpeta de trabajo del agente (main.tf y estado de Terraform).
                  Por defecto es output/ junto al código. En un contenedor se apunta a un
                  volumen para que el estado sobreviva a los reinicios.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path(os.environ.get("AGENT_OUTPUT_DIR") or Path(__file__).parent / "output")