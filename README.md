# Vídeos verticales locales: OpenAI + Shotstack

El proyecto permite validar un MP4 local, transcribir su audio, agrupar captions temporizados y preparar un render vertical 1080 × 1920. Google Drive aún no participa en este flujo.

## Qué hace el código

- Valida el MP4 con `ffprobe` y extrae audio temporal con FFmpeg en fragmentos de 15 minutos.
- Usa OpenAI `whisper-1` en formato `verbose_json`, solicitando timestamps de palabra y segmento. Si no hay tiempos por palabra, distribuye los tiempos de segmento entre sus palabras.
- Agrupa las palabras en captions consecutivos de 2 a 4 palabras conservando sus marcas temporales.
- Carga una copia del MP4 a Shotstack Ingest, crea una edición con vídeo recortado para vertical y captions visibles, solicita el render y descarga el MP4 terminado.
- Mantiene el vídeo original intacto y asigna un nombre de salida nuevo. Si ya existe, elige otro nombre; no sobrescribe.
- El audio extraído se borra al terminar. Los outputs locales se guardan en `outputs/`.

## Pruebas sin costes

Las pruebas usan únicamente datos sintéticos, mocks y funciones locales; no hacen llamadas a OpenAI/Shotstack ni consumen créditos:

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
```

El entorno verificado tiene Python 3.11, `ffmpeg`, `ffprobe`, OpenAI Python SDK 3.37.0 y Requests 2.34.2. `requirements.txt` fija versiones exactas de esas bibliotecas y sus dependencias de ejecución.

## Primer procesamiento local real

1. Coloca un MP4 de prueba en el workspace, por ejemplo `data/incoming/prueba.mp4`. No se copia ni modifica al hacerlo manualmente.
2. `OPENAI_API_KEY` ya existe en Replit Secrets. No hace falta volver a configurarla.
3. Para Shotstack hace falta que el usuario configure su propia `SHOTSTACK_API_KEY` en Replit Secrets. El proyecto no inventa ni contiene esa credencial.
4. Cuando autorices expresamente los costes y el envío del archivo a esos servicios, ejecuta:

```bash
python -m video_pipeline --process data/incoming/prueba.mp4 --authorize-paid-apis
```

El flag `--authorize-paid-apis` es una barrera intencional: la transcripción de OpenAI puede tener coste; el MP4 se sube a Shotstack Ingest y el render puede consumir créditos/coste del plan. **No se han realizado esas llamadas en esta implementación.** Sin el flag no se contacta con esos servicios. Si falta `SHOTSTACK_API_KEY`, el programa se detiene antes de llamar a OpenAI.

El entorno Shotstack puede ser `v1` o `stage`:

```bash
SHOTSTACK_ENVIRONMENT=stage python -m video_pipeline --process data/incoming/prueba.mp4 --authorize-paid-apis
```

`stage` sigue siendo una llamada remota y requiere la clave de Shotstack; no se usa automáticamente. El resultado se guarda en `outputs/<nombre>_vertical_subtitled.mp4` (o un nombre incremental si ya existe).

## Limitaciones y comprobación final

- No se ha transcrito un vídeo real ni enviado una solicitud real de render. Las pruebas validan la forma y la seguridad local del flujo, no garantizan la aceptación de cada propiedad por un proyecto Shotstack concreto ni la calidad final del encuadre/captions.
- En el estado actual no hay `SHOTSTACK_API_KEY` configurada en el proyecto. No se solicitó ni se creó una.
- El servicio remoto debe poder ingerir el MP4 y renderizarlo; el primer render real sigue pendiente de la clave de Shotstack y de tu autorización explícita.
- Google Drive continúa sin conectarse. Este flujo acepta un archivo local y no modifica permisos OAuth.

## Referencias de API

- OpenAI, transcripción con timestamps: https://platform.openai.com/docs/api-reference/audio/createTranscription
- Shotstack, edición/render: https://shotstack.io/docs/api
- Shotstack, carga local mediante URL firmada: https://shotstack.io/docs/guide/ingesting-footage/sources.md
