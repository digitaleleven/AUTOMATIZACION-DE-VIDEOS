---
name: Conversión HLG a SDR
description: Reglas verificadas para evaluar la conversión de vídeo HLG/Dolby Vision de iPhone a BT.709 en MATRIX MOTION.
---

Para convertir el *base layer* HLG de iPhone con FFmpeg, declara explícitamente la entrada BT.2020/HLG/rango limitado y la salida BT.709/rango limitado. No presentes una conversión HLG del *base layer* como render Dolby Vision completo. Hable, Mobius y las variantes directas sin tone mapping probaron límites distintos: la dominante cálida persistió. `npl=1000` y EQ pueden acercar la luminancia a un objetivo, pero no sustituyen la revisión de piel, altas luces ni balance de blancos.

**Why:** a 2,5 s, `saturation=0.90` con `rr=0.97:bb=1.03` dio YAVG 112,548; la variante `unsharp` con `saturation=0.85`, `rr=0.92:gg=0.96:bb=1.08` dio 109,05; la nueva prueba con `unsharp=5:5:0.8`, `saturation=0.92`, `rr=0.90:gg=0.95:bb=1.10` dio 111,154, todavía debajo del objetivo 118–122. Parece menos amarilla, pero no se confirmó neutralidad. La varianza Laplaciana cara/pelo subió de 97,23 a 101,14 entre variantes con varios parámetros distintos, por lo que no aísla el efecto de `unsharp`. No se encontró carta gris/blanca neutral fiable; el RPU Dolby Vision sigue sin procesar porque `libplacebo` no pudo iniciar Vulkan.

**How to apply:** no integrar ni corregir el balance de blancos por conjetura. Para resolver la dominante, conseguir una referencia neutral fiable o procesar el RPU en un entorno con Vulkan operativo; comparar piel, blancos y producto con revisión humana antes de cambiar el pipeline.

## Perfil Remotion autorizado

Para la ruta Remotion de MATRIX MOTION, la responsable autorizó fijar como perfil maestro de normalización HLG→SDR: 30 FPS; escalado Lanczos y padding a 1080×1920; `unsharp=5:5:0.8:5:5:0.0`; entrada BT.2020 HLG de rango limitado y salida BT.709 limitada con `npl=1000`; EQ `contrast=1.05:brightness=0.05:gamma=1.18:saturation=0.92`; mezcla `rr=0.92:gg=0.95:bb=1.08`; YUV420p.

**Why:** el 25 de octubre de 2026 la responsable pidió consolidar esta cadena en el procesamiento Remotion tras revisar las variantes previas.

**How to apply:** usar este perfil en la ruta Remotion, separado de la compatibilidad Shotstack. La autorización fija la configuración, pero no sustituye la revisión visual humana de piel, blancos y altas luces antes de aprobar el color.
