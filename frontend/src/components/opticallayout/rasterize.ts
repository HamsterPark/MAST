// Turns a standalone SVG file (a string) into a PNG in the browser, for the figure export: the picture is
// decoded by an Image element from a blob URL and copied onto a canvas `scale` times larger. Nothing leaves
// the page.
//
// The SVG must be standalone (no var(), no external font or picture): the export resolves the design tokens
// to colours first (lib/opticalLayout/exportStyle.ts), and an Image element would not see the page's styles.

/** Largest side of the bitmap: browsers refuse canvases much beyond this. */
const MAX_SIDE_PX = 8000;

export async function rasterizeSvg(svgText: string, width: number, height: number, scale = 3): Promise<Blob> {
  const s = Math.max(0.1, Math.min(scale, MAX_SIDE_PX / Math.max(width, height, 1)));
  const url = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const img = new Image();
    img.decoding = "async";
    const loaded = new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error("the browser could not draw the SVG"));
    });
    img.src = url;
    await loaded;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * s));
    canvas.height = Math.max(1, Math.round(height * s));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no 2-D canvas in this browser");
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("the PNG could not be made"))), "image/png"));
  } finally {
    URL.revokeObjectURL(url);
  }
}
