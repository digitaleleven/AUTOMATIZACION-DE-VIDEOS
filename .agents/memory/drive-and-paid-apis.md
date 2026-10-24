---
name: Permisos de Drive y costes de APIs
description: Límites de autorización para Google Drive, OpenAI y Shotstack en este proyecto.
---

La usuaria autorizó solicitar únicamente `drive.readonly` y `drive.file` para el flujo OAuth de la cuenta secundaria permitida. No conectar la cuenta principal ni ampliar esos scopes sin nueva autorización. No ejecutar llamadas reales a Drive ni renders de pago sin autorización separada.

**Why:** `drive.readonly` amplía la capacidad técnica del token a lectura de todo el Drive secundario, aunque la aplicación debe limitar sus operaciones a las tres carpetas configuradas; la autorización de scopes no equivale a autorizar la conexión real.

**How to apply:** solicitar solo el par de scopes aprobado, mantener comprobación server-side de la identidad secundaria y limitar las operaciones de MATRIX MOTION a 01_ENTRADA, 02_PROCESADOS y RECURSOS/LOGOS. La propietaria autoriza OAuth y cualquier prueba real por separado.

La propietaria aprobó la detección automática de vídeos nuevos en Drive sin selección manual individual. MATRIX MOTION prevé STUDIO (uso de la propietaria), CLOUD (SaaS multiempresa) y PRIVATE (instalaciones independientes); esto es contexto futuro, no autorización para implementar esos modelos ahora.

**Why:** separar la meta del producto de los límites concretos de acceso, conexión y coste evita interpretar una autorización limitada como permiso general.

**How to apply:** no ampliar los scopes aprobados ni introducir multiusuario; conservar una frontera proveedor-adaptador para futuras modalidades y mantener la revisión humana antes de renders.

La usuaria decidió conectar una segunda cuenta Gmail existente y no la cuenta principal, para que la documentación profesional o confidencial de la principal no quede expuesta a MATRIX MOTION. La identidad OAuth debe coincidir exactamente con el correo secundario configurado antes de guardar o usar tokens.

**Why:** separar la identidad de automatización evita que una selección accidental de la cuenta principal autorice el panel.

**How to apply:** verificar server-side callbacks OAuth y tokens temporales de Picker; fallar cerrado, no registrar ni mostrar correo o tokens, y no ampliar ni revocar permisos por el flujo Picker sin autorización.

Según el brief de la propietaria del 24 de octubre de 2026, la cuenta secundaria completó un escaneo real de `01_ENTRADA` y una prueba de crear y borrar un archivo temporal en `02_PROCESADOS`. Esto no demuestra que un MP4 renderizado pueda subirse y reproducirse desde la carpeta de salida.

**Why:** la prueba temporal valida una escritura básica, pero no equivale a verificar el tamaño, formato, subida y reproducción del resultado final.

**How to apply:** no repetir operaciones reales de Drive ni renders sin autorización separada; antes de declarar la integración completa, comprobar con permiso de la propietaria que un MP4 nuevo llega a `02_PROCESADOS` y se puede reproducir.
