// Text the way the server keeps it (docs/v2/design/optical_layout.md section 11.1, and
// MASTv2/mast/optical_layout/model.py): control characters are removed, and in a one-line
// field every run of whitespace becomes one space. Too long is REFUSED by the server, never
// cut, so the editor limits the length itself and says so (validate.ts); it does not
// silently shorten what the operator typed.
//
// The editor applies the same cleaning on the way in, so what it shows is what the server
// will store and a save does not come back with different text.

/** Control characters except tab, line feed and carriage return. */
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

/** One line: no control characters, every run of whitespace one space, trimmed. */
export function cleanLine(v: string): string {
  return v.replace(CONTROL, "").split(/\s+/).filter(Boolean).join(" ");
}

/** Free text: no control characters, line breaks as \n, the ends trimmed. */
export function cleanText(v: string): string {
  return v.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(CONTROL, "").trim();
}

/** Number of characters the way Python counts them (code points, not UTF-16 units). */
export function charCount(v: string): number {
  return Array.from(v).length;
}
