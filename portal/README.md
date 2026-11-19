# Portal SDN Cardamomo Gelato

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
