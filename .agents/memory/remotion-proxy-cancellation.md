---
name: Diagnóstico del proxy Remotion
description: Interpretación y diagnóstico de fallos del proxy de fotogramas OffthreadVideo.
---

No atribuir un fallo de `OffthreadVideo` al disco por el texto genérico de Chromium. Si el servidor ya envió las cabeceras y el `write` termina con `ECANCELED`, la respuesta de fotograma fue cancelada por el cliente; ese dato no identifica por sí solo por qué se cerró.

**Why:** una prueba local de MATRIX MOTION mostró que el servidor Remotion, Chromium y el compositor podían extraer y decodificar el fotograma aisladamente, mientras `renderMedia` cancelaba la respuesta. El mensaje sugería falta de disco pese a que había espacio disponible.

**How to apply:** registrar ruta y estado HTTP, eventos de Chromium y la causa del cierre en el flujo completo. Separar pruebas de archivo, compositor, proxy e integración `renderMedia`; no repetir un render basándose únicamente en el aviso de disco.
