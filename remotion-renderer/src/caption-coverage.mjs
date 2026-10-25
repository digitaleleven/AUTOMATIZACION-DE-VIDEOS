import { groupTimedWords } from "./caption-timing.mjs";

const tokens = (text) => String(text ?? "").trim().split(/\s+/u).filter(Boolean);
const normalized = (text) => tokens(text).map(
  (word) => word.normalize("NFD").replace(/\p{Diacritic}/gu, "")
    .toLowerCase().replace(/[^\p{L}\p{N}]/gu, ""),
).filter(Boolean);

// Shared windows are display intervals, NOT recovered word-level alignment.
export function prepareCaptionTrack(payload, clips, fps = 30) {
  if (!Array.isArray(payload?.words) || !payload.words.length || !tokens(payload.text).length) {
    throw new Error("La cobertura requiere texto completo y palabras de la transcripción.");
  }
  const fromText = normalized(payload.text);
  const fromWords = normalized(payload.words.map((word) => word.word).join(" "));
  if (JSON.stringify(fromText) !== JSON.stringify(fromWords)) {
    throw new Error("El texto y la lista de palabras no coinciden: se requiere revisión antes del render.");
  }
  const raw = payload.words.map((word, index) => {
    const start = Number(word.start);
    const end = Number(word.end);
    const text = String(word.word ?? "").trim();
    if (!text || !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start) {
      throw new Error(`La palabra ${index + 1} no tiene un intervalo utilizable; no se elimina silenciosamente.`);
    }
    return { text, start, end, index };
  });
  const units = [];
  const uncertainties = [];
  for (let index = 0; index < raw.length; index += 1) {
    const word = raw[index];
    const members = [word];
    if (word.start === word.end) {
      while (raw[index].start === raw[index].end && index + 1 < raw.length) {
        members.push(raw[++index]);
      }
      const anchor = members.at(-1);
      if (anchor.end <= anchor.start || anchor.start - word.start > 0.75) {
        throw new Error(`No hay ancla cercana y fiable para «${word.text}»; alineación pendiente.`);
      }
    }
    const parts = members.flatMap((member) => tokens(member.text));
    if (parts.length > 3) {
      throw new Error("El intervalo compartido supera tres palabras; necesita alineación revisada.");
    }
    const shared = members.length > 1 || parts.length > 1;
    const anchor = members.at(-1);
    const start = shared ? anchor.start : word.start;
    const end = anchor.end;
    const clip = clips.find((item) =>
      start >= item.sourceStart / fps - 1e-7 &&
      end <= (item.sourceStart + item.frames) / fps + 1e-7,
    );
    // Reject a cut through a zero-length word too, not only through its anchor.
    if (!clip || members.some((member) =>
      member.start < clip.sourceStart / fps - 1e-7 ||
      member.end > (clip.sourceStart + clip.frames) / fps + 1e-7,
    )) throw new Error(`Un corte elimina o divide «${members.map((member) => member.text).join(" ")}».`);
    const offset = (clip.outputStart - clip.sourceStart) / fps;
    const displayWords = parts.map((text) => ({
      text, start: start + offset, end: end + offset, highlight: !shared,
    }));
    if (shared) uncertainties.push({
      text: parts.join(" "), reason: members.length > 1 ? "zero-duration word; shared anchor display window" : "shared name interval",
      sourceStart: start, sourceEnd: end,
      originalIntervals: members.map(({ text, start, end }) => ({ text, start, end })),
      outputStart: start + offset, outputEnd: end + offset,
      individualHighlight: false,
    });
    units.push({ shared, words: displayWords });
  }
  const groups = [];
  let precise = [];
  const flush = () => {
    if (precise.length) groups.push(...groupTimedWords(precise, 1, 3));
    precise = [];
  };
  for (const unit of units) {
    if (unit.shared) {
      flush();
      groups.push({ words: unit.words, start: unit.words[0].start, end: unit.words[0].end });
    } else precise.push(...unit.words);
  }
  flush();
  const renderedTokens = normalized(groups.flatMap((group) => group.words.map((word) => word.text)).join(" "));
  if (JSON.stringify(renderedTokens) !== JSON.stringify(fromText)) {
    throw new Error("La pista de subtítulos pierde palabras: render bloqueado.");
  }
  // Each timed display must actually survive frame sampling and cannot hide behind another group.
  for (let index = 0; index < groups.length; index += 1) {
    const group = groups[index];
    if (Math.ceil(group.start * fps) / fps >= group.end ||
        (index && group.start < groups[index - 1].end - 1e-7)) {
      throw new Error(`El grupo «${group.words.map((word) => word.text).join(" ")}» no es visible o se solapa.`);
    }
  }
  return {
    groups,
    audit: {
      sourceTextWords: fromText.length, displayedWords: renderedTokens.length,
      omittedWords: 0, sharedWindows: uncertainties.length, uncertainties,
      audioCompleteness: "Not independently verified against speech; coverage checks the supplied transcript.",
    },
  };
}
