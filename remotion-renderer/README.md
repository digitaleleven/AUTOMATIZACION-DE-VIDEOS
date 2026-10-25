# MATRIX MOTION — prueba local aislada de Remotion

Este directorio solo contiene una prueba experimental. No se importa desde la
aplicación Python, el panel ni Shotstack. El navegador de render se ejecuta con
resolución de hosts externos bloqueada. El render no recibe clave de licencia
ni llama al servicio de seguimiento de Remotion.

## Perfil maestro 4K para revisión visual

El perfil actual conserva la resolución nativa vertical del MOV (2160 × 3840),
desactiva por completo el zoom de cámara y convierte HLG BT.2020 limitado a
BT.709 limitado. Esta conversión directa no procesa el RPU Dolby Vision y no
implica aprobación visual del color.

```sh
ffmpeg -nostdin -hide_banner -n \
  -i "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  -map 0:v:0 \
  -vf "fps=30,scale=2160:3840:force_original_aspect_ratio=decrease:flags=lanczos,pad=2160:3840:(ow-iw)/2:(oh-ih)/2,unsharp=5:5:1.0:5:5:0.0,setsar=1,zscale=primariesin=bt2020:transferin=arib-std-b67:matrixin=bt2020nc:rangein=limited:primaries=bt709:transfer=bt709:matrix=bt709:range=limited:npl=1000:c=topleft,eq=contrast=1.08:brightness=0.03:gamma=1.12:saturation=0.95,colorchannelmixer=rr=0.92:gg=0.95:bb=1.08,format=yuv420p" \
  -an -c:v libx264 -preset ultrafast -crf 14 -g 1 -keyint_min 1 -sc_threshold 0 -pix_fmt yuv420p \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  "$PWD/generated-artifacts/drive-inspection/remotion/IMG_1254-master-HQ-4K-proxy-v2.mp4"
```

La nitidez usa `unsharp=5:5:1.0:5:5:0.0`. El EQ y el canal RGB se mantienen con
los valores solicitados para esta revisión. El audio se toma aparte de la pista
AAC principal del original y se recorta en Remotion con los mismos fotogramas.
No se modifica ni sobrescribe el MOV.

## Edición completa con Remotion

La ruta completa conserva los cortes de `mm004-plan.json`, asigna las palabras
cronometradas a la línea de tiempo de salida y no aplica zoom de cámara. Los SVG
mantienen entradas y salidas suaves. El reproductor toma el vídeo del proxy 4K
y el audio principal AAC del original.

```sh
node remotion-renderer/render-full.mjs \
  --input "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  --proxy "$PWD/generated-artifacts/drive-inspection/remotion/IMG_1254-master-HQ-4K-proxy-v2.mp4" \
  --transcript "$PWD/generated-artifacts/drive-inspection/q03a/transcription-corrected.json" \
  --plan "$PWD/generated-artifacts/drive-inspection/mm004/mm004-plan.json" \
  --effects-dir "$PWD/generated-artifacts/drive-inspection/mm004/effects" \
  --output "$PWD/generated-artifacts/drive-inspection/remotion/IMG_1254-MATRIX-MOTION-Remotion-HQ-4K-v2.mp4"
```

La salida es H.264 High Profile, CRF 15/preset `medium`, 2160 × 3840, 30 FPS,
BT.709 limitado y YUV420p. Cada ejecución usa `maxRetries=0` y concurrencia 1.
El script valida proxy All-Intra, duración, audio, resolución, perfil H.264,
color y decodificación completa. Los resultados permanecen en `generated-artifacts/`,
ignorado por Git.

## Experimento Mobius anterior (conservado)

El proxy se limita a los 12 segundos de fuente que necesita la composición.
Se selecciona la primera pista AAC para no intentar decodificar pistas
adicionales y se fija a 30 FPS para que los recortes por fotograma coincidan
con Remotion:

```sh
mkdir -p generated-artifacts/drive-inspection/q03c-control
ffmpeg -y \
  -i "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  -map 0:v:0 -map 0:a:0 -t 12 \
  -vf "fps=30,zscale=primariesin=bt2020:transferin=arib-std-b67:matrixin=bt2020nc:rangein=limited:primaries=bt2020:transfer=linear:matrix=gbr:range=full:npl=100,format=gbrpf32le,tonemap=tonemap=mobius:param=0.5,zscale=primariesin=bt2020:transferin=linear:matrixin=gbr:rangein=full:primaries=bt709:transfer=bt709:matrix=bt709:range=limited:c=topleft,scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p" \
  -c:v libx264 -preset ultrafast -crf 17 -g 1 -keyint_min 1 -pix_fmt yuv420p \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -ac 2 -ar 48000 \
  "$PWD/generated-artifacts/drive-inspection/q03c-control/proxy_30fps_allintra_1080_mobius.mp4"

pnpm --dir remotion-renderer run render:sample -- \
  --input "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  --transcript "$PWD/generated-artifacts/drive-inspection/q03a/transcription-corrected.json" \
  --proxy "$PWD/generated-artifacts/drive-inspection/q03c-control/proxy_30fps_allintra_1080_mobius.mp4" \
  --render-frames 150 \
  --output "$PWD/remotion-renderer/generated-artifacts/remotion/allintra-mobius-30fps-1080.mp4"

ffmpeg -y -ss 2.5 \
  -i "$PWD/remotion-renderer/generated-artifacts/remotion/allintra-mobius-30fps-1080.mp4" \
  -frames:v 1 -update 1 \
  "$PWD/remotion-renderer/generated-artifacts/remotion/allintra-mobius-30fps-1080-frame-2.5.png"
```

La muestra usa `OffthreadVideo` y renderiza solo 150 fotogramas (5 segundos).
El proxy conserva H.264 All-Intra, 30 FPS y audio AAC; el controlador verifica
metadatos y que todos los fotogramas sean I-frames antes de abrir Chromium.
El experimento Mobius se ejecutó en RGB lineal con `param=0.5`: FFmpeg documenta la opción
`param`, no `jnc`. La escala Lanczos y el padding se aplican después de la
conversión a BT.709 para evitar escalar el material HLG antes de mapear tonos.

## Ejecución local sin proxy preparado

Desde la raíz del repositorio, indica explícitamente el MOV/MP4, el JSON de
Whisper con palabras y tiempos, y una ruta de salida nueva:

```sh
pnpm --dir remotion-renderer run render:sample -- \
  --input "$PWD/generated-artifacts/drive-inspection/IMG_1254.MOV" \
  --transcript "$PWD/generated-artifacts/drive-inspection/q03a/transcription-corrected.json" \
  --output "$PWD/remotion-renderer/generated-artifacts/remotion/sample-corrected.mp4"
```

El script valida con FFprobe el perfil de color de entrada. El modo directo
convierte HLG BT.2020 limitado a BT.709 limitado con `zscale`, omite el tone
mapping y aplica los ajustes EQ documentados arriba. Hable se conserva para la
prueba Q-02 y Mobius permanece disponible solo como alternativa de comparación;
los perfiles desconocidos se rechazan en vez de etiquetarse como BT.709. Los
proxies de muestra son Lanczos, 1080 × 1920 y 30 FPS para los 12 segundos que
cubren los segmentos; se extrae únicamente el audio AAC principal a PCM temporal.
El MOV original no se altera.

La composición recibe solo palabras con marcas temporales reales de Whisper.
Mapea las palabras completas que caen dentro de los segmentos; omite las que
cruzan un corte, muestra grupos centrados de 1 a 3 palabras y resalta en amarillo
corporativo únicamente la palabra activa. El zoom de cámara queda desactivado.
Los stickers tienen una entrada breve con interpolación suave, sin rebote brusco.
El render completo es 2160 × 3840, 30 FPS, H.264 CRF 15/preset `medium`,
BT.709 limitado y YUV420p. Chromium trabaja con concurrencia 1, cero reintentos,
un hilo de decodificación de OffthreadVideo, un máximo de 32 MiB de caché y sin
codificación paralela. Los temporales se eliminan al terminar.
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
