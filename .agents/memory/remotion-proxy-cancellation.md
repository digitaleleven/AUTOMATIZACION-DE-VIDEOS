---
name: Diagnóstico del proxy Remotion
description: Interpretación y diagnóstico de fallos del proxy de fotogramas OffthreadVideo.
---

No atribuir un fallo de `OffthreadVideo` al disco por el texto genérico de Chromium. Si el servidor ya envió las cabeceras y el `write` termina con `ECANCELED`, la respuesta de fotograma fue cancelada por el cliente; ese dato no identifica por sí solo por qué se cerró. La prueba mínima sí genera un MP4 reproducible cuando recibe un MP4 H.264/AAC optimizado a 1080 × 1920 y 25 FPS.

**Why:** una prueba local de MATRIX MOTION mostró que el servidor Remotion, Chromium y el compositor podían extraer y decodificar el fotograma aisladamente, mientras la composición compleja cancelaba la respuesta de `renderMedia`. Un render mínimo posterior con una entrada optimizada sí terminó y superó FFprobe y decodificación completa. El mensaje sugería falta de disco pese a que había espacio disponible.

**How to apply:** usar la entrada optimizada y la composición mínima como control de viabilidad; al depurar la composición completa, registrar ruta y estado HTTP, eventos de Chromium y la causa del cierre. Separar pruebas de archivo, compositor, proxy e integración `renderMedia`; no repetir un render basándose únicamente en el aviso de disco.

### Recortes de OffthreadVideo en Remotion 4

En Remotion 4.0.538, `trimBefore` usa fotogramas de origen y `durationInFrames` es el número de fotogramas que se reproducen desde ese punto. `trimAfter` es un recorte de fotogramas desde el extremo derecho y está obsoleto para este uso; no pasar segundos ni una hora final en ese campo. FFprobe y una decodificación completa pueden aprobar un MP4 que muestre imagen solo al principio y negro después, por lo que hay que revisar fotogramas representativos y comprobar señal visual.

**Why:** un render de MATRIX MOTION pasó las validaciones de códec, duración y decodificación, pero el contenido quedó negro después del primer fotograma porque el código mezclaba segundos con el recuento de fotogramas de `OffthreadVideo`.

**How to apply:** pasar índices y duraciones enteros en fotogramas, validar que el segmento no supere la duración de la fuente y añadir una comprobación de imagen visible además de FFprobe y decodificación.
