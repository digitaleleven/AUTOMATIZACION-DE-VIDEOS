---
name: Conversión HLG a SDR
description: Reglas verificadas para evaluar la conversión de vídeo HLG/Dolby Vision de iPhone a BT.709 en MATRIX MOTION.
---

Para convertir el *base layer* HLG de iPhone con FFmpeg, declara explícitamente la entrada BT.2020/HLG/rango limitado y la salida BT.709/rango limitado. No presentes una conversión HLG del *base layer* como render Dolby Vision completo. Hable, Mobius y las variantes directas sin tone mapping probaron límites distintos: la dominante cálida persistió. `npl=1000` y EQ pueden acercar la luminancia a un objetivo, pero no sustituyen la revisión de piel, altas luces ni balance de blancos.

**Why:** en el mismo recorte central a 2,5 s, `saturation=0.94` obtuvo YAVG 112,81 y `saturation=0.90` con `colorchannelmixer=rr=0.97:bb=1.03` obtuvo 112,548; la piel y el fondo aún parecen cálidos, así que no se confirmó un balance neutral. No se encontró metadato de balance de blancos ni una carta gris/blanca neutral fiable; el RPU Dolby Vision sigue sin procesar porque `libplacebo` no pudo iniciar Vulkan. Por eso la causa de la dominante no está confirmada y un ajuste manual de temperatura sería arbitrario.

**How to apply:** no integrar ni corregir el balance de blancos por conjetura. Para resolver la dominante, conseguir una referencia neutral fiable o procesar el RPU en un entorno con Vulkan operativo; comparar piel, blancos y producto con revisión humana antes de cambiar el pipeline.
