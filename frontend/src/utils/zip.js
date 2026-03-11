import JSZip from "jszip";

export async function downloadZip(artifacts) {
  const zip = new JSZip();
  artifacts.files.forEach(f => zip.file(f.path, f.content));
  const blob = await zip.generateAsync({ type: "blob" });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement("a"), {
    href: url,
    download: `${artifacts.zipName}.zip`
  });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
