---
name: Modal como motor único de render
description: Alcance de backend de render y normalización previa en MATRIX MOTION.
---

MATRIX MOTION usa Modal como único motor de render. No exponer Shotstack ni habilitar reintentos de renders antiguos. Mantener intacto el original: convertir entradas compatibles a una copia de trabajo validada antes del envío, y detenerse si el color o las pistas no se pueden interpretar con seguridad.

**Why:** la responsable eligió explícitamente Modal exclusivo y normalización de vídeos 4K/HEVC con las herramientas ya disponibles, manteniendo los renders bajo autorización humana.

**How to apply:** conservar la salida fija de 1080×1920 a 30 FPS y la normalización local con FFmpeg; no ejecutar un render ni llamar a Modal sin autorización expresa para ese trabajo.
