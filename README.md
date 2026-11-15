# SDN Cardamomo Gelato

Proyecto de la clase Software Defined Networking: red de sucursales de Cardamomo Gelato, una cadena de heladerías en Guatemala. Se construye con DMVPN de dos hubs sobre IPsec IKEv2, OSPFv2/OSPFv3, doble pila IPv4/IPv6 y monitoreo con Zabbix por SNMPv3. El ciclo de vida de cada sitio (alta, cambios y baja) se hace solo con Ansible, sin configurar los routers a mano.

## Estructura

- `playbooks/site_add.yml`: alta o reconfiguración idempotente de un router.
- `playbooks/site_remove.yml`: baja limpia de una sucursal (routers, hubs y Zabbix).
- `playbooks/zabbix_setup.yml`: importa la plantilla propia de Zabbix.
- `roles/`: `ipsec`, `dmvpn`, `lan`, `routing`, `snmp`, `zabbix` y `decommission`.
- `inventory/`: `hosts.yml` (hubs y primeras sucursales), `sitios.yml` (sitios nuevos, lo escribe el portal), `host_vars/` y `group_vars/`.
- `files/zabbix_template_sdn.yml`: plantilla con vecinos OSPF y estado de túneles.

## Direccionamiento

Todo se deriva del ID de sitio `s`: LAN `10.(11+s).0.0/24`, IPv6 `2001:1111:s::/64`, underlay `172.16.0.s`, loopback `10.254.0.s/32` y túneles `10.255.1.s` y `10.255.2.s`. El día `11` sale de la fecha de cumpleaños de un integrante, según el enunciado.

## Requisitos

- Ansible con las colecciones de `requirements.yml`: `ansible-galaxy collection install -r requirements.yml`.
- `paramiko==3.4.0`: IOS 15.7 solo ofrece intercambio de claves SHA-1.
- Zabbix 7.0 con acceso por API para el usuario de Ansible.

## Secretos

Las contraseñas no están en el repositorio. Viven en `inventory/group_vars/all/vault.yml`, un archivo cifrado con Ansible Vault que `.gitignore` excluye. Quien clone el repositorio debe crearlo con `ansible-vault create inventory/group_vars/all/vault.yml` y estas variables:

| Variable | Para qué sirve |
| --- | --- |
| `vault_ansible_password` | Contraseña del usuario `ansible` en los routers |
| `vault_ipsec_psk` | Clave compartida de IPsec |
| `vault_snmp_auth` | Contraseña de autenticación SNMPv3 |
| `vault_snmp_priv` | Contraseña de cifrado SNMPv3 |
| `vault_zabbix_user` | Usuario de la API de Zabbix |
| `vault_zabbix_password` | Contraseña de la API de Zabbix |

Los playbooks se ejecutan con `--ask-vault-pass` o con `--vault-password-file`.

## Uso

Alta de un sitio y baja de un sitio (ejemplo con el router R7):

    ansible-playbook playbooks/site_add.yml -l R7 --vault-password-file ~/.vault_pass
    ansible-playbook playbooks/site_remove.yml -l R7 --vault-password-file ~/.vault_pass

La baja exige `-l` y solo acepta spokes. Con `--check` se ven los cambios sin aplicarlos, y una segunda corrida debe dar `changed=0`.

## Notas de diseño

- La VRF `INET` del underlay se define en el Day-0, no en Ansible, para no cortar el acceso al router.
- Zabbix consulta por SNMPv3 la IP de underlay dentro de la VRF; el agente responde por la VRF de entrada y lee el contexto global.
- Los usuarios SNMPv3 no aparecen en `running-config`, por eso se verifican con `show snmp user` y se guarda con `write memory`.
- En IOL algunos comandos NHRP no aparecen en `running-config`; se aplican en tareas aparte sin marcar cambios.
