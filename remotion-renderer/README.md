# MATRIX MOTION — prueba local aislada de Remotion

Este directorio contiene el motor de composición. No se importa desde la
aplicación Python ni usa Shotstack. El navegador de render se ejecuta con
resolución de hosts externos bloqueada. El render no recibe clave de licencia
ni llama al servicio de seguimiento de Remotion.

## Máster local: Remotion para gráficos + FFmpeg para vídeo

La ruta actual es de dos etapas: Remotion dibuja una capa transparente con
subtítulos de 1–3 palabras, títulos, tarjetas/PIP, SVG y efectos de sonido, sin
abrir ni decodificar el MOV. La composición usa una base de 1080 × 1920 y se escala
al perfil vertical solicitado: 720 × 1280, 1080 × 1920 o 2160 × 3840. El perfil 4K
requiere una fuente máster de al menos 2160 × 3840.
FFmpeg lee el original como fuente maestra, ejecuta los cortes de
`mm004-plan.json`, aplica la conversión HLG BT.2020 → SDR BT.709 y compone la
capa. El original y los resultados previos no se sobrescriben.

```sh
cd remotion-renderer
pnpm run render:full
```

Salida: `outputs/IMG_1254-MATRIX-MOTION-v3-PRO-MASTER.mp4`. El informe FFprobe y
los registros quedan junto al trabajo bajo `generated-artifacts/`. La codificación
final es H.264 High, CRF 15/preset `medium`, rango limitado, BT.709 y YUV420p; el
ancho, alto y FPS son seleccionables entre los perfiles admitidos (25, 30, 50 o 60
FPS). En la interfaz web se elige 4K para másteres 4K; la CLI conserva 1080 × 1920
a 30 FPS como valor por defecto. El audio AAC estéreo se conserva y se mezcla con
efectos locales.
FFprobe y una decodificación completa se ejecutan antes de informar éxito.

La capa Remotion usa ProRes 4444 con alfa, concurrencia 1 y cero reintentos.
Chromium recibe el almacenamiento temporal mediante un enlace `/tmp/mm-*` corto
cuyo destino es el directorio temporal del workspace: evita el límite de longitud
de socket que causó el fallo anterior sin volver a colocar el bundle en el tmpfs.
El perfil directo de color es una prueba técnica; BT.709 en metadatos no significa
que el color esté aprobado visualmente.

El panel autenticado conserva `show_subtitles` en la configuración de estilo. La
casilla afecta las próximas ejecuciones locales, no modifica MP4 existentes.
`font_family`, `font_color` y `highlight_color` alimentan los subtítulos de Remotion.
El valor por defecto es Montserrat 800, texto activo oscuro `#202124` y resaltado
rosa pastel bebé `#FFC0CB`; las palabras no activas conservan blanco con contorno.
Se puede sobrescribir en una ejecución con directivas por línea en `--briefing`:

```sh
pnpm run render:full -- --briefing $'fontFamily=Montserrat\nhighlightColor=#FFC0CB\ntextColor=#202124'
```

La nota libre se conserva en el registro de la propuesta; solo esas directivas
explícitas cambian fuente o color. El render puede invocar `--show-subtitles false`
para una ejecución puntual:

```sh
pnpm run render:full -- --show-subtitles false
```

### Overlays explícitos y sincronización de productos

No se deducen marcas, descuentos ni imágenes de una nota libre. El máster acepta
`overlays` en el JSON de `--plan`:

```json
{
  "overlays": {
    "title": {"text": "Paso a paso", "duration_seconds": 3.5},
    "promo": {"text": "Código: PROMO10"},
    "images": [
      {"asset": "serum.png", "label": "Sérum", "trigger_text": "este sérum",
       "position": "right", "duration_seconds": 2.5}
    ],
    "cards": [
      {"text": "Rutina de noche", "trigger_text": "limpieza", "position": "left"}
    ]
  }
}
```

Esto es una **estructura de ejemplo**, no contenido añadido automáticamente.
Las imágenes deben existir en `--effects-dir`; se rechazan URLs, rutas fuera de
ese directorio, imágenes vacías, de más de 25 MB o de más de 16 megapíxeles.
Posiciones: `topLeft`, `topRight`, `left`, `right`, `bottomLeft`, `bottomRight`.
Se admiten 24 tarjetas/imágenes por plan, máximo tres simultáneas, sin colisiones
en la misma posición. Los títulos usan Georgia, crema `#FFFDF8`; el widget
promocional permanece en el tercio superior izquierdo y no altera el vídeo base.

También se admiten líneas explícitas en `--briefing`:

```text
title=Paso a paso
promo=Publi / Marca autorizada
pip=serum.png|Sérum|este sérum|right
card=Rutina de noche|limpieza|left
```

El disparador se busca en las palabras realmente alineadas **después de los
cortes**. Si no existe, se omite el recurso y se registra una advertencia; no se
inventan tiempos. Un plan puede fijar `start_seconds` explícitamente. Los PIP y
tarjetas tienen entrada scale/fade, esquinas de 16 px y sombra; sus inicios
disparan un pop y un reencuadre suave limitado al 106%. `showSubtitles=false`
desactiva los subtítulos y sus SFX asociados, pero no los overlays explícitos.
No se ha conectado esta ruta experimental al procesamiento Python/Shotstack;
el briefing de esa ruta no se envía por sí solo al máster local.

### Archivos grandes y pruebas locales

Antes de renderizar, la pista de subtítulos compara el texto completo con las
palabras del JSON y verifica que ningún corte suprima o divida sus intervalos.
Las palabras con duración cero no se eliminan: se muestran junto a un ancla
próxima de duración positiva en un grupo de hasta tres palabras, **sin resalte
individual**. Los nombres compuestos con un único intervalo reciben el mismo
tratamiento. Esas ventanas compartidas se registran en
`pipeline.stage1.captionCoverage.uncertainties` del informe del MP4.
No son una alineación forzada ni tiempos individuales recuperados.
Si no hay ancla cercana, falta texto, existe solapamiento o el grupo no puede
aparecer a 30 FPS, el render se bloquea en lugar de ocultar la pérdida.
La cobertura del JSON no demuestra que Whisper haya reconocido todo lo hablado;
esa comparación con el audio requiere revisión humana o alineación adicional.

El vídeo se decodifica secuencialmente con `fps,select,setpts`, sin ramas de
vídeo 4K `split/concat` que retengan fotogramas mientras esperan otros segmentos.
FFmpeg limita decodificación/codificación a dos hilos y el grafo a uno.
Remotion usa concurrencia uno, PNG/ProRes alpha y cero reintentos. Los recursos
públicos del bundle no incluyen el MOV. Temporales y perfil Chromium permanecen
en workspace mediante un enlace corto; se comprueba espacio antes del render.
Un bloqueo exclusivo evita dos másteres simultáneos. Si un proceso muere por
SIGKILL, hay que comprobar el PID de `master-render.lock` antes de retirarlo.

Los procesos locales tienen salida de diagnóstico acotada, un tiempo máximo
explícito y terminación de su grupo si se bloquean; un timeout produce un error,
no un nuevo intento. Esto reduce riesgos: no garantiza ausencia de fallos para
cualquier tamaño, duración o recursos disponibles.

```sh
npm run test:captions
npm run typecheck
# Prueba opt-in de 60 fotogramas (2 s) sobre IMG_1254.MOV, sin proveedores:
node tools/validate-local-pipeline.mjs
```

La prueba opt-in crea archivos nuevos en `generated-artifacts/pipeline-validation`,
comprueba MP4/audio/decodificación completa y hash del original, e incluye
overlays claramente identificados como prueba. No sustituye la validación de
un render completo ni su revisión humana de sonido, encuadre y color.

El demo rápido requiere que exista el máster anterior y crea un archivo nuevo:

```sh
npm run demo
```

Usa 10 segundos, 720 × 1280, H.264 CRF 22/preset `ultrafast`; no sustituye ni
reprocesa el MOV ni el máster.

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

El máster recibe marcas temporales del JSON de Whisper y valida la cobertura
completa del texto antes del render. Agrupa de 1 a 3 palabras y resalta solo la
palabra con alineación individual fiable, con texto oscuro sobre rosa pastel
`#FFC0CB`. Usa Montserrat 800 por defecto. Los SVG conservan entrada/salida suave;
el audio añade un pop por bloque de subtítulos/tarjeta y whoosh en cortes,
sin impactos graves ni campanillas por defecto. La imagen usa reencuadres
smoothstep de 1.00× a 1.06× y regreso, sin salto inicial al 108%,
en frases y pausas naturales. El reproductor
del panel apunta al MP4 final local y sigue protegido por la autenticación actual.

Para verificar los algoritmos y tipos sin renderizar:

```sh
pnpm --dir remotion-renderer run test:captions
pnpm --dir remotion-renderer run typecheck
```

Las dependencias se fijan en `package.json` y `pnpm-lock.yaml`. Los paquetes,
archivos de trabajo y medios generados se excluyen de Git y del despliegue.
