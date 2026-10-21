# Despliegue en Azure Container Apps

Guía para llevar el agente con su interfaz web a Azure Container Apps.

**Estado: documentado, no probado de punta a punta.** La imagen no se pudo construir donde se
preparó esto (no había Docker). Cada punto va marcado así:

- ✅ **Verificado** contra la documentación oficial de Azure.
- ⚠️ **Por probar**: no se pudo confirmar; revisa con `az ... --help` o prueba en una suscripción de pruebas.

Si algo falla, no insistas con el mismo comando: copia el error y compara con `--help`.

---

## 0. Antes de empezar: qué estás desplegando

El agente **crea recursos reales en tu suscripción** a petición de quien abra la página. Eso
implica tres decisiones de seguridad:

1. **Quién puede abrir la página.** Con `HOST=0.0.0.0` el servidor se niega a arrancar sin
   `ACCESS_TOKEN`. Es una clave sencilla; para algo más serio pon un login delante (sección 9).
2. **Qué permisos tiene el agente en Azure.** Terraform usa una identidad con rol sobre la
   suscripción. Dale el rol más pequeño que alcance (por ejemplo, Contributor solo sobre una
   suscripción o grupo de recursos de pruebas, nunca Owner).
3. **El estado de Terraform contiene las contraseñas generadas en texto plano.** Quien lea el
   recurso compartido de archivos donde vive `terraform.tfstate` puede verlas. Protege esa cuenta
   de almacenamiento (sin acceso público, acceso solo a quien lo necesite).

Es una herramienta de una persona a la vez: el front ejecuta una sola cosa y guarda su historial
en memoria.

---

## 1. Requisitos

- Azure CLI con la extensión de Container Apps: `az extension add --name containerapp --upgrade`
- Estar autenticado: `az login` y `az account set --subscription <id>`
- Proveedores registrados (una sola vez por suscripción):

```powershell
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

- Tu `.env` local con las variables del modelo (`AZURE_AI_ENDPOINT`, `AZURE_AI_DEPLOYMENT`,
  `AZURE_AI_API_KEY`) y `ARM_SUBSCRIPTION_ID`.

Variables de ejemplo para los comandos (ajusta nombres; los de almacenamiento deben ser únicos y en minúsculas):

```powershell
$RG       = "rg-agente-iac"
$LOCATION = "eastus2"
$ENV_NAME = "cae-agente-iac"
$APP      = "agente-iac"
$STORAGE  = "stagenteiac<sufijo-unico>"
$SHARE    = "agente-output"
```

---

## 2. Dónde vive qué

| Qué | Dónde | Por qué |
|---|---|---|
| `main.tf`, `terraform.tfstate` | Volumen de Azure Files en `/data/output` (`AGENT_OUTPUT_DIR`) | Sobrevive a reinicios y nuevas revisiones del contenedor |
| Proveedores que baja `terraform init` | Disco local `/tmp/terraform-data` (`TF_DATA_DIR`) | Son cientos de MB; en un recurso compartido de red irían lentos. Se vuelven a bajar al reiniciar |
| Claves del modelo y de Azure | Secretos de Container Apps | No van en la imagen ni en el código |

✅ Container Apps permite montar un recurso compartido de Azure Files como volumen (se define
primero en el entorno y luego en la app), solo recursos compartidos clásicos (SMB).

---

## 3. Cuenta de almacenamiento y recurso compartido para el estado

```powershell
az group create --name $RG --location $LOCATION

az storage account create --name $STORAGE --resource-group $RG --location $LOCATION `
  --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --min-tls-version TLS1_2

az storage share-rm create --resource-group $RG --storage-account $STORAGE --name $SHARE
```

⚠️ Revisa los parámetros con `az storage share-rm create --help` si tu versión de CLI difiere.

---

## 4. Identidad para que Terraform entre a Azure

Terraform (proveedor `azurerm`) necesita credenciales dentro del contenedor.

**Opción usada en esta guía: service principal con secreto** (variables `ARM_*`):

```powershell
$SUB = az account show --query id -o tsv
az ad sp create-for-rbac --name sp-agente-iac --role Contributor --scopes /subscriptions/$SUB
```

El comando devuelve `appId`, `password` y `tenant`. Guárdalos solo como secretos del paso 6:
`ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`, `ARM_SUBSCRIPTION_ID`.

> Contributor sobre toda la suscripción es amplio. Para pruebas, limita `--scopes` a un grupo de
> recursos vacío y cambia el ámbito de lo que el agente puede tocar.

⚠️ **Identidad administrada** (sin secretos): `azurerm` la admite con `ARM_USE_MSI=true`, pero no
se verificó que funcione desde dentro de Container Apps en esta guía. Si la pruebas, sería la
opción preferible.

---

## 5. Construir y crear la app

Desde la carpeta del proyecto (donde está el `Dockerfile`):

```powershell
az containerapp env create --name $ENV_NAME --resource-group $RG --location $LOCATION
```

Primer despliegue con un token provisional (se cambia por el secreto real en el paso 6):

```powershell
az containerapp up --name $APP --resource-group $RG --environment $ENV_NAME --source . `
  --ingress external --target-port 8000 --env-vars ACCESS_TOKEN=provisional
```

✅ `az containerapp up --source .` construye con el `Dockerfile` que encuentre en la carpeta y
despliega.
⚠️ La sintaxis exacta de `--env-vars` en `up` cambió entre versiones: confirma con
`az containerapp up --help`.
⚠️ La imagen no se construyó antes de escribir esta guía. Si `docker build` o el build de ACR
falla, el error estará en el `Dockerfile` (el paso de Terraform verifica la suma SHA-256: si
HashiCorp publica otra, el build se detiene a propósito).

Para otra versión de Terraform: `--build-arg TERRAFORM_VERSION=...` en un build manual.

---

## 6. Secretos y variables de entorno

```powershell
az containerapp secret set --name $APP --resource-group $RG --secrets `
  token=<un-valor-largo-y-aleatorio> `
  ai-key=<AZURE_AI_API_KEY> `
  arm-secret=<password-del-service-principal>

az containerapp update --name $APP --resource-group $RG --set-env-vars `
  ACCESS_TOKEN=secretref:token `
  AZURE_AI_API_KEY=secretref:ai-key `
  ARM_CLIENT_SECRET=secretref:arm-secret `
  AZURE_AI_ENDPOINT=<endpoint> `
  AZURE_AI_DEPLOYMENT=<deployment> `
  ARM_CLIENT_ID=<appId> `
  ARM_TENANT_ID=<tenant> `
  ARM_SUBSCRIPTION_ID=<subscription-id> `
  AZURE_FALLBACK_REGIONS=brazilsouth,swedencentral,eastus2
```

✅ Los secretos se definen en la app y las variables los referencian con `secretref:`.
⚠️ Comprueba los nombres de parámetros con `az containerapp secret set --help` y
`az containerapp update --help`.

Genera el token con algo como: `[guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")`.

---

## 7. Montar el volumen en `/data/output`

Primero en el entorno (clave de la cuenta de almacenamiento):

```powershell
$KEY = az storage account keys list --resource-group $RG --account-name $STORAGE --query "[0].value" -o tsv

az containerapp env storage set --name $ENV_NAME --resource-group $RG `
  --storage-name agente-output --azure-file-account-name $STORAGE `
  --azure-file-account-key $KEY --azure-file-share-name $SHARE --access-mode ReadWrite
```

Luego en la app, con YAML (el montaje de volúmenes se hace así):

```powershell
az containerapp show --name $APP --resource-group $RG --output yaml > app.yaml
```

Edita `app.yaml` y añade, dentro de `properties.template`:

```yaml
    containers:
      - name: agente-iac            # el bloque ya existe: solo añade volumeMounts
        volumeMounts:
          - volumeName: output
            mountPath: /data/output
    volumes:
      - name: output
        storageType: AzureFile
        storageName: agente-output
        mountOptions: uid=1000,gid=1000,dir_mode=0770,file_mode=0660
```

```powershell
az containerapp update --name $APP --resource-group $RG --yaml app.yaml
```

✅ `storageType: AzureFile`, `storageName` y `mountOptions` (separadas por comas) son parte del
esquema documentado.
⚠️ Que `uid`/`gid` en `mountOptions` den permiso de escritura al usuario no administrador del
contenedor (uid 1000) **no está documentado**. Por eso el servidor comprueba al arrancar que
puede escribir en la carpeta y, si no, se detiene con un mensaje claro. Si ves
*"No puedo escribir en la carpeta de trabajo"*, ajusta `mountOptions` (o prueba sin `uid`/`gid`)
y vuelve a desplegar. Como último recurso se puede ejecutar como root cambiando `USER` en el
`Dockerfile`, a costa de menos aislamiento.

`app.yaml` está en `.gitignore`: puede traer datos de tu suscripción, no lo subas.

---

## 8. Una sola réplica

El agente guarda en memoria el historial, la confirmación pendiente y el bloqueo de "una cosa a
la vez", y el estado de Terraform no admite dos escritores. Fija:

```powershell
az containerapp update --name $APP --resource-group $RG --min-replicas 1 --max-replicas 1
```

Recursos sugeridos: 1 CPU y 2 GiB (`--cpu 1.0 --memory 2.0Gi`). ⚠️ Es una estimación para
`terraform init` + `plan`, no una medición: ajusta si el contenedor se reinicia por memoria.

---

## 9. Proteger el acceso

El orden de menos a más protección:

1. **`ACCESS_TOKEN`** (ya incluido): la página se abre con `https://<tu-app>/?token=<valor>`.
   El token viaja en la URL: no compartas capturas de la barra de direcciones.
2. **Restricción por IP**: permitir solo tu IP o la de tu oficina. ✅ Container Apps admite
   restricciones de IP de entrada. ⚠️ El comando exacto
   (`az containerapp ingress access-restriction set ...`) conviene confirmarlo con `--help`.
3. **Autenticación con Microsoft Entra (Easy Auth)**: pone un inicio de sesión delante de la
   app. ✅ Existe para Container Apps. ⚠️ No se verificó que conviva bien con el WebSocket del
   front: pruébalo antes de depender de él.

Con cualquiera de las tres, mantén el token como segunda capa.

---

## 10. Tiempos y conexiones

- ✅ Las peticiones HTTP tienen un límite de **240 s** en Container Apps. El front usa un
  **WebSocket**, que Container Apps admite, así que un `plan` largo no se corta por eso.
- ⚠️ Si una conexión se queda inactiva mucho rato, puede cerrarse; la página reconecta y repite
  el historial y la confirmación pendiente. Si se cierra **mientras se espera tu "sí"**, el
  agente cancela (nunca aplica solo). Si pasan 10 minutos sin respuesta también cancela.
- Si la app se reinicia a mitad de un `apply`, el estado en el recurso compartido queda como
  Terraform lo dejó: vuelve a pedir y revisa el plan; no asumas que se completó.

---

## 11. Comprobaciones tras desplegar

1. `az containerapp logs show --name $APP --resource-group $RG --follow` — debe verse
   `Agente en http://0.0.0.0:8000`. Si dice que no puede escribir en la carpeta, ver sección 7.
2. Abre `https://<fqdn>/?token=<valor>` (`az containerapp show ... --query properties.configuration.ingress.fqdn -o tsv`).
3. Pide algo mínimo y barato (un grupo de recursos), revisa el plan, confirma, verifica en el portal.
4. Reinicia la app y comprueba que `destroy` sigue viendo el recurso (el estado persistió).
5. Destruye desde la página (**Destruir recursos**) antes de dar por buena la prueba.

---

## 12. Limpieza

Primero destruye lo que creó el agente (botón **Destruir recursos**). Después, lo de esta guía:

```powershell
az group delete --name $RG --yes --no-wait
az ad sp delete --id <appId>
```

---

## Resumen de lo no verificado

| Punto | Qué comprobar |
|---|---|
| Imagen | Que el build funciona (no se construyó) |
| Identidad administrada con `azurerm` | Alternativa a los secretos `ARM_*` |
| `mountOptions` uid/gid | Escritura del usuario 1000 en el volumen |
| Easy Auth + WebSocket | Que la conexión no se rompa |
| Sintaxis de `up --env-vars`, `secret set`, `ingress access-restriction` | `--help` de tu versión |
| 1 CPU / 2 GiB | Medir con una ejecución real |