---
name: Diagnóstico del proxy Remotion
description: Interpretación y diagnóstico de fallos del proxy de fotogramas OffthreadVideo.
---

No atribuir un fallo de `OffthreadVideo` al disco por el texto genérico de Chromium. Si el servidor ya envió las cabeceras y el `write` termina con `ECANCELED`, la respuesta de fotograma fue cancelada por el cliente; ese dato no identifica por sí solo por qué se cerró. La prueba mínima sí genera un MP4 reproducible cuando recibe un MP4 H.264/AAC optimizado a 1080 × 1920 y 25 FPS.

**Why:** una prueba local de MATRIX MOTION mostró que el servidor Remotion, Chromium y el compositor podían extraer y decodificar el fotograma aisladamente, mientras la composición compleja cancelaba la respuesta de `renderMedia`. Un render mínimo posterior con una entrada optimizada sí terminó y superó FFprobe y decodificación completa. El mensaje sugería falta de disco pese a que había espacio disponible.

**How to apply:** usar la entrada optimizada y la composición mínima como control de viabilidad; al depurar la composición completa, registrar ruta y estado HTTP, eventos de Chromium y la causa del cierre. Separar pruebas de archivo, compositor, proxy e integración `renderMedia`; no repetir un render basándose únicamente en el aviso de disco.
