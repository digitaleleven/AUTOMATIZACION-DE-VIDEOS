---
name: Audio de overlays parciales en Remotion
description: Comportamiento del audio al renderizar capas ProRes Remotion con un rango parcial de fotogramas.
---

Un render de gráficos ProRes con un rango parcial puede no incluir pista de audio cuando ninguna secuencia de efectos sonoros cruza los fotogramas solicitados. El ensamblado no debe asumir que la capa gráfica siempre trae audio: si el rango no contiene efectos, conserva el audio original por separado; si contiene efectos, mezcla su pista.

**Why:** una muestra inicial de 10 segundos no contenía efectos sonoros y Remotion produjo una capa ProRes solo de vídeo. La validación que exigía una pista de audio detuvo el montaje antes de unir el audio fuente.

**How to apply:** en renders parciales, comprueba si hay efectos sonoros dentro del rango antes de exigir audio en la capa; verifica que el MP4 final conserve el audio original y se decodifique por completo.
