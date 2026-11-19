# Portal SDN Cardamomo Gelato

## Instalación local

El portal requiere Linux o WSL2, Python 3.10 o posterior y SQLite 3.35 o
posterior. Desde `portal/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n config.example.yml config.yml
cp -n .env.example .env
```

Configura las credenciales locales del portal en `.env`. Para asegurar
la continuidad de las sesiones al reiniciar, asigna una clave aleatoria
propia a `PORTAL_SECRET_KEY`. Luego inicializa los datos de ejemplo y
arranca Flask:

```bash
python seed.py
python -m pytest -q
python -m flask --app app run
```

Abre `http://127.0.0.1:5000/`. El seed puede repetirse y conserva sitios,
asignaciones e historial. Para una prueba desechable, usa las rutas
temporales de los apartados siguientes.

## IPAM y SQLite

Desde `portal/`, activa el entorno virtual e instala los requisitos con
`python -m pip install -r requirements.txt`. SQLite debe ser 3.35 o posterior.

Usa `config.yml` para configurar el direccionamiento y la cuarentena. Si
todavía no tienes ese archivo, copia `config.example.yml` a `config.yml`.
El ejemplo usa el direccionamiento del contrato y una cuarentena de siete
días. Puedes definir `PORTAL_CONFIG_PATH` y `PORTAL_DB_PATH` para elegir
otros archivos. Por defecto, `seed.py` usa `portal.sqlite3` en esta carpeta.

El seed registra R1 y R2 como hubs, R3–R6 como sucursales fijas y R7–R10
como disponibles en los datos de ejemplo locales. Confirma el estado real
del pool antes de usar estos datos con la infraestructura. Ejecutar el seed
varias veces conserva los sitios, las asignaciones y su bitácora.

### Comprobación manual con una base temporal

```bash
export PORTAL_DB_PATH="$(mktemp -d)/ipam.sqlite3"
python seed.py
python - <<'PY'
from ipam import IPAM

ipam = IPAM()
for nombre, codigo in [("Guate", "GUA"), ("Jutiapa", "JUT")]:
    sitio = ipam.reservar_sitio(nombre, codigo)
    print(sitio["router"], sitio["estado"], ipam.calcular_direcciones(sitio["site_id"])["lan_v4"])
print(ipam.calcular_direcciones(10)["lan_v6"])
PY
unset PORTAL_DB_PATH
```

La salida muestra R7 y R8 en estado `aprovisionando`, con las redes
`10.18.0.0/24` y `10.19.0.0/24`. La última línea muestra
`2001:1111:10::/64`.

`finalizar_alta(site_id, codigo_salida)` registra el resultado del alta.
`retirar_sitio(site_id)` inicia la baja y
`finalizar_baja(site_id, codigo_salida)` registra el resultado. Un código
distinto de cero deja el sitio en estado `fallido` y conserva sus recursos.
Una baja exitosa inicia la cuarentena. La siguiente reserva libera las
cuarentenas vencidas, que también se pueden procesar mediante
`liberar_vencidos()`. Las asignaciones liberadas se conservan en el historial.

Ejecuta `python -m pytest -q` para comprobar el IPAM y el login.

## Formulario y listado de sitios

Con las credenciales del Paso 1 en `.env`, inicia el portal desde `portal/`:

```bash
export PORTAL_DB_PATH="$(mktemp -d)/ipam.sqlite3"
python seed.py
python -m flask --app app run
```

Abre `http://127.0.0.1:5000/` e inicia sesión. El listado muestra cuatro
sucursales activas y cuatro routers disponibles. En **Nuevo sitio**, registra
`Escuintla` con código `ESCUINTLA` e interfaz `Ethernet0/1`. El listado muestra
R7 en estado **Aprovisionando**. Registra otro sitio con un código distinto
para comprobar que recibe R8. Si repites un código, el formulario muestra el
error y conserva los datos ingresados.

Las reservas de este paso se guardan localmente en SQLite. El contador de
routers disponibles refleja los nodos disponibles registrados en la base.

## Detalle de sitios y consultas IPAM

Con la base temporal y el portal iniciados como en el apartado anterior,
abre **Sitios** y haz clic en **Mazate**. Verás la ficha de R3 y sus
asignaciones en estado **En uso**. Si registras **Escuintla**, su ficha
mostrará las asignaciones de R7 en estado **Reservado**.

Abre **IPAM** desde la navegación. Elige **R7**, **LAN IPv6** y **Reservado**
para ver `2001:1111:7::/64`. **Limpiar filtros** vuelve a mostrar todas las
asignaciones. Si se reutiliza un router después de la cuarentena, las
asignaciones históricas siguen visibles con estado **Liberado** y su
enlace abre la ficha original.

## Archivos para Ansible

El alta publica `inventory/host_vars/Rn.yml` con `site_id`, `site_name` y
`lan_if`. Reconstruye `inventory/sitios.yml` desde SQLite con los routers
gestionados en aprovisionamiento, activos, en retirada o con una operación
fallida. Un router retirado queda fuera del inventario dinámico.

Para comprobar los YAML, inicia el portal con una base y un directorio de
desarrollo nuevos:

```bash
export PORTAL_DB_PATH="$(mktemp -d)/ipam.sqlite3"
export PORTAL_DRY_RUN=1
export PORTAL_DRY_RUN_DIR="$(mktemp -d)"
python seed.py
python -m flask --app app run
```

Registra **Escuintla** y **Jutiapa**. Abre su ficha y expande las vistas
previas en **Archivos para Ansible**. En otra terminal, sustituye la ruta por
el valor de `PORTAL_DRY_RUN_DIR` de la terminal donde arrancaste el portal.
Puedes imprimir ese valor antes de iniciar Flask con
`echo "$PORTAL_DRY_RUN_DIR"`.

```bash
cat "$PORTAL_DRY_RUN_DIR/inventory/host_vars/R7.yml"
cat "$PORTAL_DRY_RUN_DIR/inventory/sitios.yml"
```

El primer archivo debe contener las tres variables de Escuintla. El segundo
debe listar R7 y R8 una vez cada uno en `all.children.spokes.hosts`.

El modo de desarrollo es el predeterminado y escribe en `.ansible-dev/`
si no se define `PORTAL_DRY_RUN_DIR`. Para una VM autorizada,
`PORTAL_DRY_RUN=0` habilita la publicación en `inventory/` bajo
`PORTAL_ANSIBLE_DIR` o bajo `ansible_dir` de `config.yml`. Las rutas relativas
se resuelven desde `portal/`.

Los archivos se reemplazan atómicamente de forma individual. Si falla una
escritura, se restauran los archivos publicados durante esa alta y se revierte
la reserva. Day-0 queda pendiente de la plantilla oficial.

## Seguimiento del aprovisionamiento

Usa una base temporal y arranca el portal con `PORTAL_DRY_RUN=1` siguiendo
los comandos del apartado anterior. Registra una sucursal y abre su ficha.
En **Qué se hizo**, pulsa **Simular avance**. Cada pulsación completa una
etapa. Con siete etapas verás **64 %** y con once, **100 %**. Recarga la ficha
o reinicia Flask con la misma base temporal para comprobar la persistencia.

En otra sucursal, pulsa **Simular fallo** durante el avance. La etapa en
ejecución aparece como **Fallida** y las siguientes quedan pendientes.
La simulación guarda su resultado en SQLite y el sitio conserva su estado
y sus asignaciones IPAM.

Con una sesión iniciada, puedes consultar `GET /sitios/<id>/progreso` para
obtener las etapas, el porcentaje y las últimas líneas del seguimiento en
JSON. Cuando exista una ejecución, también puedes consultarla con
`GET /runs/<id>/progress`. `progress.py` define la función
`aplicar_evento` para que un futuro proceso registre transiciones de etapas
mediante identificador de ejecución y orden. La plantilla oficial Day-0
sigue pendiente.

## Aprovisionamiento automático

La configuración predeterminada es `PORTAL_DRY_RUN=1`. Cualquier valor
distinto de `0` o `1` se rechaza al arrancar.

Para probar el alta automática, inicia Flask con una base y un inventario
temporales como en **Archivos para Ansible**. Registra una sucursal, abre su
ficha y pulsa **Aprovisionar**. El progreso avanza en segundo plano. Prueba
**Tarea fallida** o **Router inalcanzable** en el selector de resultados.
Las fichas y el listado identifican los resultados simulados, que se guardan
en la base de desarrollo. Usa otra base de datos para producción.

### Configuración para la VM

Con `PORTAL_DRY_RUN=0`, configura `PORTAL_ANSIBLE_DIR`,
`PORTAL_ANSIBLE_PLAYBOOK_BIN` y `PORTAL_VAULT_PASSWORD_FILE`. También se
admiten sus campos equivalentes `ansible_dir`, `ansible_playbook_bin` y
`vault_password_file` en `config.yml`. Las rutas relativas del ejecutable y
del Vault se resuelven desde el proyecto Ansible. El archivo de contraseña
debe estar fuera del proyecto con permisos `0600`, y `ansible-vault` debe
estar junto a `ansible-playbook`.

Define `PORTAL_DAY0_READY_ROUTERS` con los routers ya confirmados por el
operador, separados por comas, por ejemplo `R7,R8`. El portal valida el
inventario y comprueba que el Vault se pueda abrir sin guardar su contenido.
También comprueba alcance TCP/22 antes de lanzar `site_add.yml`. La
plantilla oficial Day-0 sigue pendiente y la prueba TCP no autentica SSH.

Los procesos Flask de un mismo portal deben compartir la base SQLite y su
archivo de bloqueo adyacente. Solo se ejecuta una alta a la vez. El plazo
máximo es de diez minutos. SQLite conserva los eventos reconocidos, los
conteos de Ansible y el resultado de la ejecución. Una ejecución que quede
interrumpida se registra como fallida cuando el portal recupera el bloqueo.

## Baja, cuarentena e historial

Con `PORTAL_DRY_RUN=1`, registra una sucursal y aprovisiónala con el
resultado **Éxito**. Abre su ficha, escribe su código exacto y pulsa
**Dar de baja**. El progreso sigue las tareas de `decommission`. Si la
baja se completa, la ficha indica el inicio y el fin de cuarentena y el
portal quita el router del inventario dinámico.

Con **Tarea fallida**, la ficha ofrece **Reintentar baja**. Las direcciones
y ambos archivos se conservan. Si el playbook termina bien y falla el
cierre local, el portal conserva un recibo en SQLite y el reintento solo
completa el inventario y la cuarentena. Los códigos internos negativos
`-127` y `-128` indican, respectivamente, cierre local pendiente y resultado
remoto sin confirmación durable. Para `-128`, conserva la reserva y verifica
la VM y SQLite antes de cualquier intervención. Una caída entre la respuesta
de Ansible y la escritura del recibo también puede exigir reconciliación.

Una reserva posterior libera las cuarentenas vencidas. Para liberar una
cuarentena antes, escribe el código del sitio nuevamente en la ficha. La
acción queda registrada y la ficha anterior conserva su historial cuando
otro sitio recibe el mismo router. Consulta **Bitácora** para filtrar por
router, operación o resultado. El **Historial de operaciones** de la ficha
enlaza a cada intento y su salida resumida.

## Instalación en la VM de Ansible

1. Usa el repositorio existente de Ansible y coloca el portal en su carpeta
   `portal/`. Instala Python, crea el entorno virtual del portal e instala
   sus dependencias como en la instalación local. En el entorno de Ansible
   instala las colecciones declaradas en el `requirements.yml` del proyecto.
2. Prepara una base SQLite de producción independiente. Configura
   `PORTAL_DB_PATH`, `PORTAL_ANSIBLE_DIR`, `PORTAL_ANSIBLE_PLAYBOOK_BIN` y
   `PORTAL_VAULT_PASSWORD_FILE`. Mantén la contraseña del Vault fuera del
   repositorio y verifica los permisos y la accesibilidad del inventario.
3. Antes de inicializar el pool de producción, confirma el estado real de
   sus routers con el responsable de Ansible. Los estados de `seed.py` son
   datos de ejemplo. Si ya hay un sitio gestionado en la VM, reconcilia su
   reserva y el inventario antes de habilitar operaciones.
4. Obtén la plantilla Day-0 oficial y confirma cada router preparado. Antes
   de la primera alta, coordina con el responsable de la VM la validación de
   Zabbix mediante `test_zabbix.yml` y `zabbix_setup.yml`.
5. Activa `PORTAL_DRY_RUN=0` para la integración autorizada. Comprueba en la
   VM un alta, una baja y un fallo controlado. Los procesos que sirven el
   portal deben compartir SQLite, el archivo de bloqueo y el inventario.

Quedan pendientes la plantilla Day-0, la confirmación de la copia de
seguridad prevista por la guía y la validación de extremo a extremo con
la VM, los routers y Zabbix.
