---
name: Conversión HLG a SDR
description: Reglas verificadas para evaluar la conversión de vídeo HLG/Dolby Vision de iPhone a BT.709 en MATRIX MOTION.
---

Para convertir el *base layer* HLG de iPhone con FFmpeg, declara explícitamente la entrada BT.2020/HLG/rango limitado y la salida BT.709/rango limitado. No presentes una conversión HLG del *base layer* como render Dolby Vision completo. Hable, Mobius y las variantes directas sin tone mapping probaron límites distintos: la dominante cálida persistió y la combinación `npl=1000` con EQ pudo subexponer mucho el plano. Reducir saturación global no equivale a corregir balance de blancos.

**Why:** las muestras comparadas en el mismo instante conservaron el tinte cálido con Mobius y con EQ. La conversión directa con `npl=1000`, `contrast=0.92`, `brightness=-0.05`, `gamma=0.85`, `saturation=0.92` bajó el YAVG del recorte central a 70,6, frente a 154,8 con Hable y 187,4 con Mobius. No se encontró metadato de balance de blancos ni una carta gris/blanca neutral fiable; el RPU Dolby Vision sigue sin procesar porque `libplacebo` no pudo iniciar Vulkan. Por eso la causa de la dominante no está confirmada y un ajuste manual de temperatura sería arbitrario.

**How to apply:** no integrar ni corregir el balance de blancos por conjetura. Para resolver la dominante, conseguir una referencia neutral fiable o procesar el RPU en un entorno con Vulkan operativo; comparar piel, blancos y producto con revisión humana antes de cambiar el pipeline.
