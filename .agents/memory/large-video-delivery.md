---
name: Entrega de vídeos grandes
description: Límite de adjuntos y entrega de másteres sin recomprimir.
---

El registro de activos mediante `presentAsset` tiene un límite observado de 100 MiB (104857600 bytes). Para un máster mayor, entregar un enlace de reproducción/descarga autenticado de la app; no reducir calidad ni volver a renderizar solo para adjuntarlo sin autorización.

**Why:** la entrega de un máster válido fue rechazada por tamaño, no por corrupción del vídeo. La entrega por enlace preserva el resultado y evita trabajo duplicado.

**How to apply:** verificar primero archivo y tamaño, mantener autenticación y soporte de rangos de vídeo, y distinguir decodificación local aprobada de reproducción dentro de la sesión de la propietaria, que no se comprueba con el navegador de captura sin iniciar sesión.
