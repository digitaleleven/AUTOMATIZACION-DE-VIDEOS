---
name: Permisos de Drive y costes de APIs
description: Límites de autorización para Google Drive, OpenAI y Shotstack en este proyecto.
---

No ampliar los permisos de Google Drive ni ejecutar llamadas de pago o renders reales sin autorización explícita de la usuaria.

**Why:** la usuaria estableció este límite para evitar cambios de acceso o cargos no aprobados en la automatización de vídeo.

**How to apply:** mantener las pruebas con archivos sintéticos y dobles locales; exigir autorización explícita antes de cualquier llamada real y no ampliar OAuth al trabajar en la integración de Drive.

La propietaria aprobó como objetivo de producto la detección automática de vídeos nuevos en Drive, sin selección manual individual. Esa aprobación no autoriza por sí sola cambios de código, scopes, conexión de cuentas ni llamadas a APIs. MATRIX MOTION prevé STUDIO (uso de la propietaria), CLOUD (SaaS multiempresa) y PRIVATE (instalaciones independientes); esto es contexto futuro, no autorización para implementar esos modelos ahora.

**Why:** la auditoría separa la aprobación del objetivo de automatización de las decisiones de seguridad y ejecución que aún requieren aprobación.

**How to apply:** diseña la propuesta actual para la detección automática, pero espera aprobación explícita antes de implementar o cambiar permisos; conserva una frontera proveedor-adaptador para futuras modalidades sin introducir multiusuario ahora.

La usuaria decidió conectar una segunda cuenta Gmail existente y no la cuenta principal, para que la documentación profesional o confidencial de la principal no quede expuesta a MATRIX MOTION. La identidad OAuth debe coincidir exactamente con el correo secundario configurado antes de guardar o usar tokens.

**Why:** separar la identidad de automatización evita que una selección accidental de la cuenta principal autorice el panel.

**How to apply:** exigir verificación server-side del correo de Google y fallar cerrado si falta la cuenta permitida; no registrar ni mostrar el correo o tokens, y no ampliar scopes sin nueva autorización.
