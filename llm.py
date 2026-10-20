"""Único punto de contacto con el modelo de IA (Azure AI Foundry).

Cambiar de proveedor o de modelo significa tocar solo este archivo.
Contrato: recibe texto y devuelve texto.

Usa la API "v1" de Azure OpenAI (https://<recurso>.openai.azure.com/openai/v1/),
la que Foundry muestra como "Punto de conexión de Azure OpenAI". No necesita api-version.

Configuración por variables de entorno (ver .env.example), nunca en el código:
  AZURE_AI_ENDPOINT    https://<recurso>.openai.azure.com/  (con o sin /openai/v1)
  AZURE_AI_DEPLOYMENT  nombre del despliegue del MODELO en Foundry (no el de un agente)
  AZURE_AI_API_KEY     clave del recurso
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno {name} (ver .env.example).")
    return value


def build_base_url(endpoint: str) -> str:
    """Normaliza el endpoint a https://<recurso>.openai.azure.com/openai/v1/"""
    base = endpoint.strip().rstrip("/")
    if not base.endswith("/openai/v1"):
        base += "/openai/v1"
    return base + "/"


def ask_model(system_prompt: str, user_text: str) -> str:
    """Envía el prompt del sistema y la petición del usuario; devuelve el texto de la respuesta."""
    client = OpenAI(
        base_url=build_base_url(_require("AZURE_AI_ENDPOINT")),
        api_key=_require("AZURE_AI_API_KEY"),
    )
    response = client.chat.completions.create(
        model=_require("AZURE_AI_DEPLOYMENT"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )
    return (response.choices[0].message.content or "").strip()