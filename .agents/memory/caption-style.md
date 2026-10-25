---
name: Ritmo y estilo de subtítulos de MATRIX MOTION
description: Regla de agrupación y resaltado de palabras habladas en las ediciones Remotion.
---

MATRIX MOTION muestra como máximo tres palabras a la vez. El valor visual de Remotion es Montserrat 800, caja rosa pastel bebé `#FFC0CB` para la palabra activa, texto activo oscuro de alto contraste, posición vertical al 75% y pop de 1.15× a 1× durante tres fotogramas. Mantener redondeo, padding amplio y sombra; permitir que Configuración y directivas explícitas del briefing sobrescriban familia tipográfica y colores.

**Why:** la responsable cambió el límite a tres palabras y sustituyó el rosa neón por rosa pastel bebé para un estilo «clean girl», manteniendo el pop.

**How to apply:** mantener el límite en vistas previas y renders completos; usar solo marcas temporales fiables y los valores `fontFamily`, `highlightColor` y `textColor` configurados o declarados expresamente en el briefing.

No equiparar cobertura del texto con exactitud del reconocimiento del audio. Las palabras con duración cero y los nombres compuestos pueden mostrarse con una ventana compartida, sin fingir resaltado individual.

**Why:** se observó que Whisper entregó palabras de duración cero que un parser estricto descartaba, y que las correcciones de nombres conservaron intervalos compartidos en lugar de una alineación nueva.

**How to apply:** bloquear omisiones del texto y cortes que afecten palabras; dejar registradas las incertidumbres y no afirmar alineación perfecta ni transcripción completa del audio sin evidencia adicional.

El movimiento de cámara se limita a 100% → 106% → 100%, sin salto inicial al 108%. Usar pops discretos por bloque/tarjeta y whoosh en los cortes, no impactos graves ni campanillas añadidos por defecto.

**Why:** la responsable fijó estas reglas en las directrices de estética «clean girl».

**How to apply:** mantener los reencuadres ligados a frases/productos y el límite de escala aun al añadir nuevos recursos.
