---
name: Conversión HLG a SDR
description: Reglas verificadas para evaluar la conversión de vídeo HLG/Dolby Vision de iPhone a BT.709 en MATRIX MOTION.
---

Para convertir el *base layer* HLG de iPhone con FFmpeg, declara explícitamente la entrada BT.2020/HLG/rango limitado, el intermedio RGB lineal completo y la salida BT.709/rango limitado. En el material evaluado, un valor positivo de `tonemap=desat` neutralizó piel y altas luces; `desat=0` es el candidato de revisión, no una aprobación de producción. No presentes una conversión HLG del *base layer* como render Dolby Vision completo.

**Why:** las muestras controladas a 8, 25 y 49 s mostraron SATAVG de 21,8–23,8 con Hable `desat=0`, frente a 1,3–1,5 con `desat=0.2`; una muestra real de 10 s con etiquetas BT.709 y decodificación completa correcta confirmó el candidato. Aun con rango limitado declarado, aparecieron unos pocos valores Y por encima de 235, así que las etiquetas por sí solas no demuestran ausencia de superblancos. `libplacebo` tampoco pudo iniciar porque el entorno carecía de Vulkan.

**How to apply:** mantener el tono de color fuera del pipeline de producción hasta la revisión visual humana. Validar tanto metadatos como niveles de luminancia/superblancos. Si se requiere usar el RPU Dolby Vision, comprobar primero `libplacebo` en un entorno con Vulkan operativo y verificar el resultado con muestras visuales; no inferirlo solo por los metadatos.
