# Informe de pruebas en Azure real

Proyecto: Agent-IaC-Terraform · Fecha de las pruebas: 20 de octubre de 2026

## Resumen

Se probó el agente en una suscripción gratuita de Azure, un servicio a la vez, destruyendo lo
creado al terminar. De los siete servicios nuevos, **cinco quedaron verificados** y **dos
quedaron bloqueados por límites de la suscripción**, no por el código del agente.

| Servicio | Resultado | Región | Evidencia |
|---|---|---|---|
| Cosmos DB | Verificado | eastus2 | `enableFreeTier = true`, una sola región |
| Key Vault | Verificado | eastus2 | RBAC activo, sin protección contra purga, SKU standard |
| Container Apps | Verificado | eastus2 | Imagen de `mcr.microsoft.com`, CPU 0.25, 0.5Gi, 1 réplica, tope de logs 0.5 GB/día |
| Azure SQL | Verificado | brazilsouth | `sqladmin`, TLS 1.2, base Basic de 2 GB |
| Red básica | Verificado (dentro de los intentos de VM) | eastus2 y brazilsouth | Red virtual, subred, NSG, asociación e interfaz creadas |
| App Service | Bloqueado | eastus2 y brazilsouth | Cuota 0 para F1 y B1 |
| Máquina virtual | Bloqueada (la red sí se creó) | eastus2 y brazilsouth | `SkuNotAvailable` para Standard_B1s |

Servicios ya verificados antes de esta ronda: grupo de recursos y cuenta de almacenamiento.

## Preparación

- Las pruebas automáticas fallaban por un archivo mal nombrado (`service_check.py` en lugar de
  `service_checks.py`). Tras renombrarlo, quedaron dos fallas en `test_deploy.py` que
  esperaban el orden anterior de regiones; se actualizaron las expectativas.
- Resultado final: `OK`.
- Se registró el proveedor `Microsoft.App`, necesario para Container Apps.

## Resultados por servicio

### Cosmos DB
Cuenta creada en un grupo `rg-ventas-dev`. Comprobado por CLI: free tier activo y una única
región. Captura: `docs/portal-cosmosdb.png`.

### Key Vault
Bóveda `kv-app-dev-...` con sufijo aleatorio. Comprobado por CLI: `rbac = true`,
`purga = null`, `sku = standard`. Captura: `docs/portal-keyvault.png`.

### Container Apps
Se crearon Log Analytics, entorno y aplicación. Comprobado por CLI: imagen de
`mcr.microsoft.com/k8se/quickstart`, 0.25 CPU, 0.5Gi, máximo 1 réplica, tope diario de logs de
0.5 GB y URL pública con la página de ejemplo. Primero hubo que registrar `Microsoft.App`.
Captura: `docs/portal-container-apps.png`.

### Azure SQL
En `eastus2` Azure respondió `ProvisioningDisabled: Provisioning is restricted in this
region`. En `brazilsouth` se crearon servidor y base. Comprobado por CLI: login `sqladmin`,
TLS 1.2, región `brazilsouth`, SKU Basic y 2 GB. Captura: `docs/portal-sql.png`.

Durante la verificación hubo una inconsistencia transitoria de Azure: una consulta dijo que el
servidor no existía y un minuto después respondió bien. Algo parecido ocurrió con el primer
`destroy`, que falló con un 404 al leer el cifrado de la base.

### Red básica
Se creó completa (red virtual, subred, NSG, asociación e interfaz de red) como parte de los
dos intentos de la máquina virtual, en `eastus2` y en `brazilsouth`.

### App Service (bloqueado)
`terraform validate` pasó a la primera y el plan era correcto, pero el apply falló con
`Current Limit (F1 VMs): 0` y luego `Current Limit (B1 VMs): 0`. Se repitió el mismo error
creando el plan directamente con `az appservice plan create` en `brazilsouth`, sin pasar por el
agente. Es un límite de la suscripción. Para desbloquearlo hay que solicitar cuota en el portal
(Uso y cuotas, `Microsoft.Web`); no se comprobó qué efecto tendría en el crédito gratuito.

### Máquina virtual (bloqueada)
El plan era correcto: sin IP pública, usuario `azureuser`, contraseña de `random_password`
oculta, disco Standard. La red se creó, pero Azure rechazó la VM con `SkuNotAvailable ...
Capacity Restrictions: Standard_B1s` en `eastus2` y en `brazilsouth`. La cuota de vCPUs era
suficiente (4 en las tres regiones revisadas), así que es falta de capacidad. Sin probar:
`swedencentral` y los tamaños B1ms y B2s.

## Hallazgos sobre el agente

1. **Choque de nombres con estado no vacío (3 veces: `rg-web-dev`, `rg-app-dev`,
   `rg-ventas-dev`).** Si queda algo en el estado de Terraform y el modelo genera nombres de
   recursos distintos, el plan destruye el grupo viejo y crea el nuevo con el mismo nombre al
   mismo tiempo, y Azure responde "already exists". El plan lo advertía con el aviso de
   destrucción. Arreglo propuesto: que el agente se niegue a generar algo nuevo mientras el
   estado no esté vacío y pida `destroy` primero.
2. **Bloque `locals` rechazado** por la lista cerrada (dos veces con la VM). Con el reintento el
   modelo no lo volvió a usar. Arreglo propuesto: añadir al prompt una regla que prohíba
   `locals`, `variable` y `output`.
3. **Errores de región no reconocidos como tales.** Los mensajes de cuota (`Current Limit`),
   `ProvisioningDisabled` y `SkuNotAvailable` no mencionan la región en el texto. En los
   registros revisados no se llegó a ver la oferta automática de región alterna. Mejora
   propuesta: ampliar la detección en `regions.py`.
4. **Los atributos de `azurerm` 4 escritos de memoria resultaron correctos** para Cosmos DB,
   Key Vault, Container Apps, SQL, red y VM (pasaron `validate` y `plan`).

## Información útil de la suscripción

- Cuota de máquinas virtuales: 4 vCPUs regionales y 4 de la familia B en `eastus2`,
  `brazilsouth` y `swedencentral`.
- `NetworkWatcherRG` lo crea Azure automáticamente al usar redes virtuales; no pertenece al
  agente y se puede ignorar.
- `proyecto-foundry` es el grupo del modelo de IA y no debe tocarse.

## Pendientes

1. Probar la VM en `swedencentral` o con tamaño B2s; si no, dejarla como bloqueada.
2. Decidir sobre App Service: pedir cuota o dejarlo documentado como límite.
3. Corregir el agente (hallazgos 1 a 3) y añadir pruebas.