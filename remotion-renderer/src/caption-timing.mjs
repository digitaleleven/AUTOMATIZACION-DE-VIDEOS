const SENTENCE_END = /[.!?…]["'”’)\]]*$/u;
const normalizeWord = (text) =>
  text
    .toLocaleLowerCase("es")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/[^\p{L}\p{N}]/gu, "");

const SPANISH_STOP_WORDS = new Set([
  "a", "al", "algo", "como", "con", "cuando", "de", "del", "donde", "el",
  "ella", "ellos", "en", "era", "es", "esa", "ese", "eso", "esta", "este",
  "esto", "fue", "ha", "hay", "la", "las", "le", "les", "lo", "los", "más",
  "me", "mi", "muy", "no", "nos", "o", "para", "pero", "por", "que", "se",
  "sin", "si", "sí", "son", "su", "sus", "te", "tu", "tú", "un", "una",
  "uno", "y", "ya",
].map(normalizeWord));

export function parseWhisperWordTimings(payload) {
  const rawWords = payload && typeof payload === "object" ? payload.words : null;
  if (!Array.isArray(rawWords)) {
    throw new Error("El JSON de Whisper debe incluir marcas temporales por palabra.");
  }

  const words = rawWords
    .map((item) => {
      const text = typeof item?.word === "string" ? item.word.trim() : "";
      const start = Number(item?.start);
      const end = Number(item?.end);
      return { text, start, end };
    })
    .filter(
      (word) =>
        word.text.length > 0 &&
        Number.isFinite(word.start) &&
        Number.isFinite(word.end) &&
        word.start >= 0 &&
        word.end > word.start,
    )
    .sort((left, right) => left.start - right.start || left.end - right.end);

  if (words.length === 0) {
    throw new Error("El JSON de Whisper no contiene palabras con tiempos válidos.");
  }
  return words;
}

export function mapWordTimingsToOutput(words, segments, fps = 50) {
  if (!Number.isFinite(fps) || fps <= 0) {
    throw new Error("FPS debe ser un número positivo.");
  }

  const mapped = [];
  for (const segment of segments) {
    const { sourceStart, frames, outputStart } = segment;
    if (
      !Number.isFinite(sourceStart) ||
      !Number.isFinite(frames) ||
      !Number.isFinite(outputStart) ||
      sourceStart < 0 ||
      frames <= 0 ||
      outputStart < 0
    ) {
      throw new Error("El plan de edición contiene un segmento inválido.");
    }

    const sourceStartSeconds = sourceStart / fps;
    const sourceEndSeconds = (sourceStart + frames) / fps;
    const outputStartSeconds = outputStart / fps;

    for (const word of words) {
      // Keep only whole, reliably aligned words; never invent timing across a cut.
      if (
        word.start < sourceStartSeconds ||
        word.end > sourceEndSeconds ||
        word.end <= word.start
      ) {
        continue;
      }
      mapped.push({
        text: word.text,
        start: outputStartSeconds + word.start - sourceStartSeconds,
        end: outputStartSeconds + word.end - sourceStartSeconds,
      });
    }
  }
  return mapped.sort((left, right) => left.start - right.start || left.end - right.end);
}

export function isKeywordWord(text) {
  if (typeof text !== "string") return false;
  const normalized = normalizeWord(text);
  return normalized.length >= 4 && !SPANISH_STOP_WORDS.has(normalized);
}

export function keywordWindowsForSegment(words, segment, fps = 30) {
  if (
    !Number.isFinite(fps) ||
    fps <= 0 ||
    !Number.isFinite(segment?.outputStart) ||
    !Number.isFinite(segment?.frames) ||
    segment.frames <= 0
  ) {
    throw new Error("El segmento para zoom de palabras clave no es válido.");
  }

  const outputStartSeconds = segment.outputStart / fps;
  const outputEndSeconds = outputStartSeconds + segment.frames / fps;
  return words
    .filter(
      (word) =>
        isKeywordWord(word.text) &&
        word.start >= outputStartSeconds &&
        word.end <= outputEndSeconds &&
        word.end > word.start,
    )
    .map((word) => {
      const startFrame = Math.max(
        0,
        Math.round((word.start - outputStartSeconds) * fps),
      );
      return {
        startFrame,
        endFrame: Math.max(
          startFrame + 1,
          Math.round((word.end - outputStartSeconds) * fps),
        ),
      };
    });
}

function partitionSentence(words, maxWords) {
  const groups = [];
  let offset = 0;
  let remaining = words.length;
  while (remaining > maxWords) {
    const size = remaining - maxWords === 1 ? maxWords - 1 : maxWords;
    groups.push(words.slice(offset, offset + size));
    offset += size;
    remaining -= size;
  }
  if (remaining > 0) groups.push(words.slice(offset));
  return groups;
}

export function groupTimedWords(words, minWords = 1, maxWords = 3) {
  if (!Number.isInteger(minWords) || !Number.isInteger(maxWords) || minWords < 1 || maxWords < minWords) {
    throw new Error("Los límites de palabras por subtítulo no son válidos.");
  }

  const sentences = [];
  let currentSentence = [];
  for (const word of words) {
    currentSentence.push(word);
    if (SENTENCE_END.test(word.text)) {
      sentences.push(currentSentence);
      currentSentence = [];
    }
  }
  if (currentSentence.length) sentences.push(currentSentence);

  const groups = [];
  let carry = [];
  for (const sentence of sentences) {
    const combined = [...carry, ...sentence];
    carry = [];
    if (combined.length < minWords) {
      carry = combined;
      continue;
    }
    groups.push(...partitionSentence(combined, maxWords));
  }

  if (carry.length) {
    const last = groups.at(-1);
    if (last && last.length + carry.length <= maxWords) {
      last.push(...carry);
    } else if (carry.length >= minWords) {
      groups.push(...partitionSentence(carry, maxWords));
    } else if (last?.length === maxWords) {
      groups.push([last.pop(), ...carry]);
    } else {
      groups.push(carry);
    }
  }

  return groups.map((groupWords) => ({
    words: groupWords,
    start: groupWords[0].start,
    end: groupWords[groupWords.length - 1].end,
  }));
}

export function activeCaptionGroup(groups, seconds) {
  let active = null;
  for (const group of groups) {
    if (seconds >= group.start && seconds < group.end) active = group;
  }
  return active;
}

export function activeWordIndex(group, seconds) {
  for (let index = group.words.length - 1; index >= 0; index -= 1) {
    const word = group.words[index];
    if (seconds >= word.start && seconds < word.end) return index;
  }
  return -1;
}
