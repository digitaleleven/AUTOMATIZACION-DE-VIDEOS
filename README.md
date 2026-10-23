# Automatización de vídeos verticales

Esqueleto seguro para un pipeline de vídeos MP4 de Google Drive → OpenAI → Shotstack → Google Drive.

## Estado actual

- Configurados como constantes los IDs de las carpetas de entrada, procesados y logos.
- Implementada la planificación `--dry-run`: detecta archivos `.mp4` locales y muestra las fases previstas.
- La prueba local usa solo la biblioteca estándar de Python. No hace llamadas a Internet, Drive, OpenAI ni Shotstack, no lee contenido multimedia y no cambia los archivos.
- El procesamiento real todavía no está implementado. El comando normal falla de forma explícita en lugar de simular un procesamiento.
- La presencia de `OPENAI_API_KEY` se detecta sin imprimir su valor. No se necesita ni se consulta durante el dry run.

## Prueba mínima

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
```

Para inspeccionar un directorio local sin copiar ni mover nada:

```bash
python -m video_pipeline --dry-run --input-dir ./data/incoming
```

La búsqueda es solo del primer nivel del directorio. La carpeta `data/incoming` no se crea automáticamente.

## Google Drive: acceso a carpetas existentes sin ampliar scopes

El scope OAuth actual `drive.file` no equivale a acceso general a carpetas existentes: da acceso a archivos concretos que el usuario abre/selecciona o que la aplicación crea. No debe intentarse resolver cambiándolo a `drive` ni autorizando scopes adicionales.

La opción de menor alcance es mantener el OAuth actual y usar **Google Picker** para que el usuario seleccione explícitamente cada archivo existente que la aplicación debe procesar. La selección debe hacerse con el mismo proyecto OAuth que usa la automatización. Seleccionar una carpeta no debe darse por equivalente a acceso automático a todos sus archivos o a los que se añadan después: hay que probar el comportamiento con la API. En particular, no se puede afirmar que el proceso vaya a detectar sin intervención nuevos vídeos de Entrada mientras solo tenga autorización por archivo. Para probar el destino, crear únicamente un archivo nuevo de prueba en Procesados; no mover, renombrar ni sobrescribir originales. Si se necesita procesar automáticamente cualquier archivo nuevo de una carpeta existente, detenerse y solicitar autorización para acordar otro mecanismo; no ampliar permisos ni cambiar la compartición sin esa aprobación.

Actualmente no hay un token/credencial de Google en Replit Secrets, así que no se puede afirmar que el proyecto ya acceda a Drive. No pegar credenciales en el chat; cuando se decida el método autorizado, guardarlas únicamente en Secrets y leerlas desde el entorno.

## Seguridad y siguiente implementación

1. Implementar un cliente Drive de solo lectura para enumerar y descargar los MP4 originales y los logos.
2. Transcribir con OpenAI y convertir segmentos temporizados en clips de subtítulos.
3. Enviar a Shotstack una composición vertical 9:16 y esperar el render.
4. Subir el render como archivo nuevo a la carpeta Procesados. Nunca usar `move`, `delete`, `update` sobre los originales ni reutilizar el nombre/ID original para sobrescribirlo.
5. Añadir idempotencia y limpieza controlada de temporales antes de habilitar ejecución automática.

No se ha conectado Google Drive ni Shotstack, no se ha ejecutado OpenAI y no se han realizado operaciones de pago.

