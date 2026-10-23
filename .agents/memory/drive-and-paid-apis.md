---
name: Permisos de Drive y costes de APIs
description: Límites de autorización para Google Drive, OpenAI y Shotstack en este proyecto.
---

No ampliar los permisos de Google Drive ni ejecutar llamadas de pago o renders reales sin autorización explícita de la usuaria.

**Why:** la usuaria estableció este límite para evitar cambios de acceso o cargos no aprobados en la automatización de vídeo.

**How to apply:** mantener las pruebas con archivos sintéticos y dobles locales; exigir autorización explícita antes de cualquier llamada real y no ampliar OAuth al trabajar en la integración de Drive.
