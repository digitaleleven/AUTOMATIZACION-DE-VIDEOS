# MATRIX MOTION — prueba local aislada de Remotion

Este directorio solo contiene una prueba experimental. No se importa desde la
aplicación Python, el panel ni Shotstack. El navegador de render se ejecuta con
resolución de hosts externos bloqueada. El render no recibe clave de licencia
ni llama al servicio de seguimiento de Remotion.

## Ejecución única

Desde la raíz del repositorio, indica explícitamente la fuente y una ruta de
salida nueva:

```sh
pnpm --dir remotion-renderer run render:sample -- \
  --input "$PWD/data/incoming/video.mov" \
  --output "$PWD/remotion-renderer/generated-artifacts/remotion/sample.mp4"
```

El script comprueba la entrada con FFprobe, trabaja sobre una copia temporal,
produce 250 fotogramas (5 segundos, 1080 × 1920, 50 FPS), conserva el original,
valida el MP4 con FFprobe y realiza una decodificación de lectura con FFmpeg.
No reintenta el render. Los registros completos, informe y medición de recursos
se guardan junto al MP4; los fallos conservan `error.log`.

Las dependencias se fijan en `package.json` y `pnpm-lock.yaml`. Los paquetes,
archivos de trabajo y medios generados se excluyen de Git y del despliegue.
