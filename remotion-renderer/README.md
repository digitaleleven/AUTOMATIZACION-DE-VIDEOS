# MATRIX MOTION — prueba local aislada de Remotion

Este directorio solo contiene una prueba experimental. No se importa desde la
aplicación Python, el panel ni Shotstack. El navegador de render se ejecuta con
resolución de hosts externos bloqueada. El render no recibe clave de licencia
ni llama al servicio de seguimiento de Remotion.

## Ejecución única

Desde la raíz del repositorio, indica explícitamente el MOV/MP4, el JSON de
Whisper con palabras y tiempos, y una ruta de salida nueva:

```sh
pnpm --dir remotion-renderer run render:sample -- \
  --input "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  --transcript "$PWD/generated-artifacts/drive-inspection/q03a/transcription-corrected.json" \
  --output "$PWD/remotion-renderer/generated-artifacts/remotion/sample-corrected.mp4"
```

El script valida con FFprobe el perfil de color de entrada. Para HLG BT.2020
limitado usa una conversión explícita HLG → RGB lineal → Hable (`desat=0`) →
BT.709 limitado; para SDR acepta BT.709 con rango declarado. Rechaza perfiles
desconocidos en vez de etiquetarlos como si fueran BT.709. Crea un proxy
temporal Lanczos de 1080 × 1920 y 50 FPS para los 12 segundos que cubren los
segmentos, y extrae únicamente el audio AAC principal a PCM temporal. El MOV
original no se altera.

La composición recibe solo palabras con marcas temporales reales de Whisper.
Mapea las palabras completas que caen dentro de los segmentos; omite las que
cruzan un corte y resalta en amarillo corporativo únicamente la palabra activa.
El render produce 285 fotogramas (5,7 segundos, 1080 × 1920, 50 FPS) en H.264
CRF 17/preset `medium`, BT.709 limitado y YUV420p. Chromium trabaja con
concurrencia 1, cero reintentos, dos hilos FFmpeg para OffthreadVideo, un
máximo de 32 MiB de caché de vídeo y sin codificación paralela; el proxy también
limita a dos hilos el procesado FFmpeg. Los temporales se eliminan al terminar.
FFprobe comprueba duración, dimensiones, FPS, códec, pixel format, etiquetas de
color y audio, FFmpeg decodifica la salida completa y se verifica que el área
central no quede negra en la mayoría de los fotogramas. Los registros e informe
se guardan junto al MP4; los fallos conservan `error.log`.

Para verificar los algoritmos y tipos sin renderizar:

```sh
pnpm --dir remotion-renderer run test:captions
pnpm --dir remotion-renderer run typecheck
```

Las dependencias se fijan en `package.json` y `pnpm-lock.yaml`. Los paquetes,
archivos de trabajo y medios generados se excluyen de Git y del despliegue.
