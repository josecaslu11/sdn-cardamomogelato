
# Proyecto SDN Cardamomo Gelato

## Objetivo

Desarrollar un portal web de orquestación de sucursales con
Flask, SQLite e integración con Ansible, siguiendo la guía
de desarrollo y el contrato de integración Portal-Ansible.

## Entorno y tecnologías

- Sistema de desarrollo: Ubuntu sobre WSL2.
- Backend: Python 3 y Flask.
- Frontend: HTML, CSS y JavaScript.
- Base de datos: SQLite.
- Automatización: Ansible.
- Pruebas: pytest.
- Editor: Visual Studio Code.
- Control de versiones: Git y GitHub.
- Idioma de explicaciones: español.

## Organización del repositorio

El repositorio es compartido y contiene dos componentes:

- portal/: aplicación web e IPAM.
- inventory/: inventarios y variables de Ansible.
- playbooks/: automatizaciones de Ansible.
- roles/: configuraciones de infraestructura.
- files/: recursos utilizados por Ansible.

Todo el desarrollo del portal debe realizarse dentro de
portal/.

Los archivos de Ansible pueden consultarse como referencia,
pero no deben modificarse sin autorización explícita.

## Reglas de desarrollo

1. Seguir la guía del portal paso a paso.
2. Implementar únicamente la funcionalidad solicitada.
3. Mantener separada la lógica de Flask, IPAM y Ansible.
4. Explicar cada archivo creado o modificado.
5. Agregar pruebas automatizadas con pytest.
6. No modificar archivos ajenos al alcance de la tarea.
7. Mantener compatibilidad con el contrato Portal-Ansible.
8. Reutilizar componentes existentes antes de crear nuevos.
9. Mantener actualizada la documentación del portal.
10. Evitar dependencias y complejidad innecesarias.

## Integración con Ansible

- Utilizar los playbooks reales del repositorio.
- No crear un proyecto Fake Ansible.
- El portal gestiona el IPAM, los estados y las asignaciones.
- El portal genera inventory/host_vars/Rn.yml y
  inventory/sitios.yml según el contrato.
- El portal ejecuta los playbooks de alta y baja.
- Ansible configura IPsec, DMVPN, LAN, OSPF, SNMP y Zabbix.
- No duplicar en Python la lógica de configuración de Ansible.
- La integración real se realizará en la VM de Ansible.
- Ejecutar Ansible mediante subprocess con argumentos
  separados y sin shell=True.
- Permitir una sola ejecución simultánea.
- Establecer un tiempo límite de 10 minutos por ejecución.
- Registrar el resultado de cada operación en la bitácora.

## Desarrollo y pruebas locales

- Implementar PORTAL_DRY_RUN=1 para simular resultados
  de Ansible sin conectarse a la infraestructura.
- El modo simulado debe estar claramente identificado.
- No ejecutar altas ni bajas reales sin autorización.
- No modificar el inventario real durante pruebas locales.
- Utilizar archivos temporales para probar la generación
  de inventarios.
- Simular resultados exitosos, fallidos y routers
  inalcanzables.
- Validar las transiciones de estados mediante pytest.

## IPAM y gestión de routers

- R1 y R2 son hubs y no pueden darse de baja.
- R3, R4, R5 y R6 son sucursales existentes.
- R7, R8, R9 y R10 conforman el pool administrable.
- No asumir disponibilidad real sin validarla.
- Respetar las fórmulas de direccionamiento del contrato.
- Evitar asignaciones duplicadas.
- Mantener la cuarentena configurable.
- No liberar recursos si una baja de Ansible falla.

## Configuración y portabilidad

- No utilizar rutas absolutas específicas de un usuario.
- Resolver las rutas respecto al repositorio cuando sea posible.
- Permitir configurar rutas mediante variables de entorno.
- Utilizar config.yml para configuraciones no sensibles.
- Utilizar .env para credenciales y parámetros locales.
- Mantener un archivo .env.example sin secretos reales.
- El portal debe poder ejecutarse tanto en WSL como
  en la VM Linux de Ansible.

## Seguridad

- Nunca almacenar secretos o contraseñas en el código.
- Nunca subir .env, .vault_pass ni entornos virtuales.
- No mostrar credenciales en logs ni mensajes de error.
- Validar todas las entradas del usuario.
- Proteger las rutas privadas mediante autenticación.
- Consultar antes de ejecutar operaciones destructivas.

## Git y colaboración

- Trabajar directamente en la rama main.
- Mantener los cambios del portal dentro de portal/.
- No realizar commits que incluyan cambios de Ansible
  ajenos al desarrollo del portal.
- No ejecutar git push, merge o cambios destructivos
  de Git sin autorización explícita.
- Proponer mensajes de commit descriptivos.
- Verificar git status antes de preparar un commit.

## Forma de trabajo

Antes de cada implementación:

1. Revisar los archivos existentes.
2. Comprender el estado actual del proyecto.
3. Identificar los requisitos del paso solicitado.
4. Proponer los cambios necesarios.

Durante la implementación:

1. Implementar únicamente el paso solicitado.
2. Respetar la arquitectura existente.
3. Agregar o actualizar las pruebas correspondientes.

Al finalizar:

1. Ejecutar las pruebas.
2. Explicar los archivos creados o modificados.
3. Indicar cómo verificar manualmente el resultado.
4. Informar errores y tareas pendientes.
5. Proponer un mensaje de commit.
6. Esperar autorización antes de avanzar al siguiente paso.

## Estado actual del proyecto

- Entorno WSL2 configurado.
- Ansible instalado.
- Inventario y tareas de Ansible inspeccionados.
- Paso 1: aplicación Flask con login completado.
- Portal integrado al repositorio compartido.
- Siguiente paso: implementación del IPAM y base de datos.

## Documentación de referencia

La carpeta docs/ contiene los documentos originales del proyecto:

- Guía de desarrollo del portal.
- Contrato de integración Portal-Ansible.
- Enunciado oficial del proyecto.
- Guía de playbooks de Ansible.

Consultar estos documentos antes de implementar cada paso.
En caso de discrepancias técnicas, priorizar el contrato de
integración y consultar al usuario.

Esta carpeta es exclusivamente local y no debe subirse a GitHub.
Si no está disponible, solicitar los documentos necesarios.