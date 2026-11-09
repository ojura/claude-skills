// Resource bytes are embedded by bundle.py for direct file:// playback.
const element = document.getElementById("derivation-bundle");
const embedded = element ? JSON.parse(element.textContent) : null;
let workerFailure;

function encodedAsset(path) {
  const value = embedded.assets[path.replace(/^\.\//, "")];
  if (typeof value !== "string") throw new Error("Missing embedded asset: " + path);
  return value;
}

function unpack(path) {
  const binary = atob(encodedAsset(path));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

async function readBytes(path) {
  if (embedded) return unpack(path);
  const response = await fetch(path);
  if (!response.ok) throw new Error("Could not load " + path + ": " + response.status);
  return new Uint8Array(await response.arrayBuffer());
}

export function configurePdfWorker(pdfjs) {
  if (!embedded) {
    pdfjs.GlobalWorkerOptions.workerSrc = new URL("./vendor/pdf.worker.mjs", document.baseURI).href;
    return;
  }
  // Chrome rejects a Blob module worker from an opaque file origin.
  const url = "data:text/javascript;base64," + encodedAsset("vendor/pdf.worker.mjs");
  const worker = new Worker(url, { type: "module", name: "derivation-pdfjs" });
  workerFailure = new Promise((_, reject) => {
    worker.addEventListener("error", (event) => {
      reject(new Error("The embedded PDF.js worker failed" +
                       (event.message ? ": " + event.message : " to load")));
    }, { once: true });
  });
  pdfjs.GlobalWorkerOptions.workerPort = worker;
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    worker.terminate();
  });
}

export async function readManifest() {
  if (embedded) return embedded.manifest;
  const response = await fetch("./manifest.json");
  if (!response.ok) throw new Error("Missing generated presentation manifest");
  return response.json();
}

export async function openPdf(pdfjs, path) {
  const loading = pdfjs.getDocument({
    data: await readBytes(path), cMapPacked: true, enableXfa: false,
  }).promise;
  return workerFailure ? Promise.race([loading, workerFailure]) : loading;
}

export async function readVectors(path) {
  if (!("DecompressionStream" in window)) {
    throw new Error("This browser needs DecompressionStream support to play the packed vector timeline");
  }
  const stream = new Blob([await readBytes(path)]).stream()
    .pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).json();
}
