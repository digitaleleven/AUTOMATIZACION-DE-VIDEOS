export const DEFAULT_CAPTION_STYLE = Object.freeze({
  fontFamily: "Montserrat",
  highlightColor: "#FF2A85",
  textColor: "#FFFFFF",
});

const BRIEFING_DIRECTIVES = {
  fontfamily: "fontFamily",
  highlightcolor: "highlightColor",
  textcolor: "textColor",
};

export function resolveCaptionStyle(style = {}, briefing = "") {
  if (!style || typeof style !== "object" || Array.isArray(style)) {
    throw new Error("La configuración de subtítulos debe ser un objeto.");
  }
  const result = {
    fontFamily: style.font_family ?? DEFAULT_CAPTION_STYLE.fontFamily,
    highlightColor: style.highlight_color ?? DEFAULT_CAPTION_STYLE.highlightColor,
    textColor: style.font_color ?? DEFAULT_CAPTION_STYLE.textColor,
  };
  if (
    typeof result.fontFamily !== "string" ||
    !/^[A-Za-z0-9 ,'"-]{1,80}$/u.test(result.fontFamily) ||
    !/^#[\da-f]{6}$/iu.test(result.highlightColor) ||
    !/^#[\da-f]{6}$/iu.test(result.textColor)
  ) {
    throw new Error("font_family, highlight_color o font_color no tienen un formato válido.");
  }

  for (const line of String(briefing || "").split(/\r?\n/u)) {
    const match = line.match(/^\s*(fontFamily|highlightColor|textColor)\s*[:=]\s*(.*?)\s*$/iu);
    if (!match) continue;
    const key = BRIEFING_DIRECTIVES[match[1].toLowerCase()];
    const value = match[2];
    if (key === "fontFamily") {
      if (!/^[A-Za-z0-9 ,'"-]{1,80}$/u.test(value)) {
        throw new Error("fontFamily del briefing debe ser un nombre de fuente sencillo.");
      }
    } else if (!/^#[\da-f]{6}$/iu.test(value)) {
      throw new Error(`${match[1]} del briefing debe ser un color hexadecimal #RRGGBB.`);
    }
    result[key] = key === "fontFamily" ? value : value.toUpperCase();
  }
  return result;
}
