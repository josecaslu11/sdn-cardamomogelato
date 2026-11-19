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
