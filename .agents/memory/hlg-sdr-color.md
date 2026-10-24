---
name: Conversión HLG a SDR
description: Reglas verificadas para evaluar la conversión de vídeo HLG/Dolby Vision de iPhone a BT.709 en MATRIX MOTION.
---

Para convertir el *base layer* HLG de iPhone con FFmpeg, declara explícitamente la entrada BT.2020/HLG/rango limitado, el intermedio RGB lineal completo y la salida BT.709/rango limitado. No presentes una conversión HLG del *base layer* como render Dolby Vision completo. La responsable rechazó Q03E por una dominante amarilla; Q03F mostró que Mobius y Reinhard también conservan la dominante, aunque elevan más la luminancia.

**Why:** las muestras controladas confirmaron que `desat=0.2` elimina excesivamente la crominancia, pero Q03F comprobó visualmente que cambiar la curva de tone mapping no corrige por sí solo la dominante. No se encontró metadato de balance de blancos ni una carta gris/blanca neutral fiable; el RPU Dolby Vision sigue sin procesar porque `libplacebo` no pudo iniciar Vulkan. Por eso la causa de la dominante no está confirmada y un ajuste manual de temperatura sería arbitrario.

**How to apply:** no integrar ni corregir el balance de blancos por conjetura. Para resolver la dominante, conseguir una referencia neutral fiable o procesar el RPU en un entorno con Vulkan operativo; comparar piel, blancos y producto con revisión humana antes de cambiar el pipeline.
