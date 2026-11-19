
# Proyecto SDN Cardamomo Gelato

## Objetivo
Desarrollar un portal web de orquestación de sucursales con Flask,
SQLite e integración con Ansible.

## Entorno
- Sistema: Ubuntu sobre WSL2.
- Desarrollo: Python 3 y Flask.
- Base de datos: SQLite.
- Automatización: Ansible.
- Editor: VS Code conectado a WSL.
- Idioma de explicaciones: español.

## Reglas de desarrollo
1. Seguir la guía del portal paso a paso.
2. No implementar todo el proyecto de una sola vez.
3. Explicar cada archivo creado o modificado.
4. Mantener separada la lógica de Flask, IPAM y Ansible.
5. Agregar pruebas con pytest.
6. No modificar archivos ajenos al proyecto.
7. No almacenar contraseñas ni secretos en Git.
8. Pedir confirmación antes de ejecutar cambios destructivos.
9. Mantener compatibilidad con el contrato Portal–Ansible.

## Integración Ansible
- El portal genera archivos host_vars y sitios.yml.
- El portal ejecuta playbooks de alta y baja.
- Ansible configura IPsec, DMVPN, LAN, OSPF, SNMP y Zabbix.
- La integración real se realiza en la VM de Ansible.
- Durante el desarrollo se utilizará un simulador.

## Forma de trabajo
Antes de cada implementación:
1. Revisar archivos existentes.
2. Proponer los cambios.
3. Implementar únicamente el paso solicitado.
4. Ejecutar pruebas.
5. Explicar los resultados.
