---
name: Modal como motor único de render
description: Alcance de backend de render y normalización previa en MATRIX MOTION.
---

MATRIX MOTION usa Modal como único motor de render. No exponer Shotstack ni habilitar reintentos de renders antiguos. Mantener intacto el original: convertir entradas compatibles a una copia de trabajo validada antes del envío, y detenerse si el color o las pistas no se pueden interpretar con seguridad.

**Why:** la responsable eligió Modal como motor único y autorizó preparar salidas verticales de hasta 2160×3840 y 60 FPS para másteres compatibles; los renders siguen sujetos a aprobación humana.

**How to apply:** seleccionar 2160×3840 solo si el máster tiene al menos esas dimensiones; aceptar únicamente 25, 30, 50 o 60 FPS. El cambio local de código no demuestra que el worker esté desplegado ni que un render remoto sea viable. No desplegar ni renderizar sin autorización expresa para esa operación.
