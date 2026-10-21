# Imagen del agente con su front web. Pensada para Azure Container Apps (linux/amd64).
# Guía completa: docs/despliegue-container-apps.md
FROM python:3.13-slim

# Versión de Terraform. Se puede cambiar al construir: --build-arg TERRAFORM_VERSION=...
ARG TERRAFORM_VERSION=1.16.6

# Terraform: se descarga de HashiCorp y se verifica contra su archivo de sumas SHA-256
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && cd /tmp \
 && curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
 && grep " terraform_${TERRAFORM_VERSION}_linux_amd64.zip\$" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c - \
 && unzip -q "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -d /usr/local/bin terraform \
 && rm -rf /tmp/* /var/lib/apt/lists/* \
 && terraform version

# El agente no corre como administrador
RUN useradd --create-home --uid 1000 agent

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=agent:agent *.py ./
COPY --chown=agent:agent static ./static

# /data/output: main.tf y estado de Terraform (en Container Apps se monta aquí un volumen de Azure Files)
# /tmp/terraform-data: los proveedores que descarga `terraform init`; es pesado, así que va en disco local
RUN mkdir -p /data/output /tmp/terraform-data \
 && chown -R agent:agent /data /tmp/terraform-data

ENV HOST=0.0.0.0 \
    PORT=8000 \
    AGENT_OUTPUT_DIR=/data/output \
    TF_DATA_DIR=/tmp/terraform-data \
    PYTHONUNBUFFERED=1

USER agent
EXPOSE 8000

# Con HOST=0.0.0.0 el servidor se niega a arrancar si no hay ACCESS_TOKEN.
CMD ["python", "server.py"]