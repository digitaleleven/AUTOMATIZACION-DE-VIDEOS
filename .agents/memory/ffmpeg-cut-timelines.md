---
name: Semántica de cortes FFmpeg
description: Distinguir intervalos eliminados de tramos conservados al editar con trim/concat y overlays.
---

Al traducir un plan de pausas o cortes a FFmpeg, primero identifica si cada par inicio/fin representa lo que se elimina o lo que se conserva. Si representa eliminación, calcula el complemento para obtener los tramos retenidos y aplica exactamente esos mismos tramos al vídeo y al audio. Antes de codificar, comprueba que su duración suma la duración editada prevista; después valida con FFprobe la duración, el número de fotogramas y las pistas, y decodifica el archivo completo. Un código de salida 0 no demuestra que la salida esté completa.

Mantén los streams de overlays con PTS desde cero durante toda la duración de salida; controla su visibilidad con ventanas `enable` y fundidos absolutos. Así los gráficos no deben definir ni acortar la duración del vídeo base.

**Why:** Un plan de intervalos que debían eliminarse se interpretó como intervalos que debían conservarse. FFmpeg terminó sin error, pero produjo solo 1,71 segundos de contenido más el redondeo de fotograma. La inspección del filtro `trim` y la coincidencia con la suma de esos intervalos identificó la causa; no era el framesync del overlay.

**How to apply:** Al usar planes de silencios, cortes o retenciones con FFmpeg `trim`/`atrim` y `concat`, transforma explícitamente los intervalos y valida duración y fotogramas antes de presentar el resultado.
