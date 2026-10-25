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

### Proxy de OffthreadVideo y avisos de Chromium

Un proxy H.264 de 30 FPS con todos los fotogramas I puede superar FFprobe y una decodificación completa, pero aun así fallar cuando Remotion solicita el primer fotograma por su endpoint local `/proxy`. El mensaje genérico de Chrome sobre disco no demuestra falta de espacio; `ECANCELED` en la escritura tampoco identifica por sí solo la causa.

En Remotion 4.0.538, el servidor de OffthreadVideo registra el mismo mensaje para errores de extracción y de escritura HTTP. Si el registro además dice que los encabezados ya se enviaron y muestra `write ECANCELED`, el código del servidor ya había generado el fotograma y comenzado la respuesta 200; el consumidor cerró o canceló el flujo durante la escritura. La sugerencia de ampliar disco se añade genéricamente ante `Failed to fetch` y no demuestra falta de espacio.

En una prueba local posterior, el proxy All-Intra 4K volvió a fallar al entregar el fotograma 0, mientras que un proxy All-Intra 1080p a los mismos 30 FPS sí completó el render. Esto respalda el uso de 1080p para la composición 1080p, pero no demuestra por sí solo un OOM ni una saturación concreta del búfer.

**Why:** la prueba 4K registró cancelación del cuerpo HTTP con 248 GB libres; la prueba 1080p terminó con 150 fotogramas decodificables y sin fotogramas negros detectados.

**How to apply:** emparejar resolución del proxy con la composición de salida y validar imagen y audio; atribuir fallos a memoria solo con métricas de memoria u OOM, no por el aviso genérico de Chrome.

### Rutas temporales y socket de Chromium

Cuando Remotion necesita guardar bundles grandes en el disco del workspace, no asignes `TMPDIR` directamente a una ruta larga del proyecto: Chromium puede detenerse antes de renderizar con `Socket path too long` al crear `SingletonSocket`. Un enlace simbólico de nombre corto bajo `/tmp` puede apuntar al directorio de trabajo del workspace, manteniendo los datos grandes en disco y el nombre que Chromium usa para el perfil temporal corto.

**Why:** un intento real trasladó el bundle al workspace para evitar el tmpfs, pero Chromium rechazó la ruta larga del perfil antes de producir fotogramas. El directorio temporal fallido llegó a 1.1 GB; el espacio libre seguía amplio.

**How to apply:** usar un enlace temporal corto bajo `/tmp` como `TMPDIR`, apuntado al directorio temporal del workspace; eliminarlo al salir y conservar el registro del fallo. Verificar con la ruta exacta del error antes de atribuirlo a memoria o a espacio.

### ProRes 4444 alpha en Remotion

En Remotion 4.0.538, `renderMedia` con `codec: "prores"`, perfil 4444 y `pixelFormat: "yuva444p10le"` puede producir un archivo MOV que FFprobe reporta como `yuva444p12le`. La opción `imageFormat: "png"` es necesaria para capturar fotogramas transparentes; el formato final puede ser de 12 bits aunque se haya solicitado entrada de 10 bits.

**Why:** una validación estricta de `pix_fmt === "yuva444p10le"` rechazó una capa ProRes 4444 que sí conservaba alfa, audio y todos los fotogramas.

**How to apply:** validar el códec/perfil ProRes 4444 y aceptar las salidas alfa `yuva444p10le` y `yuva444p12le`; comprobar los metadatos reales con FFprobe antes de componer con FFmpeg.
