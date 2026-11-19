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
