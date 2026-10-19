"""Build a portable single HTML demo. Standard Python only, no build dependencies."""
from pathlib import Path
import base64
import re

ROOT = Path(__file__).resolve().parent.parent


def data_uri(path, mime):
    return f"data:{mime};base64," + base64.b64encode((ROOT / path).read_bytes()).decode()


html = (ROOT / "index.html").read_text()
css = (ROOT / "styles.css").read_text()
js = '\n'.join((ROOT / name).read_text() for name in ('content.js', 'pdf-reader.js', 'ai-reader.js', 'ask-reader.js', 'app.js'))

for path in ("assets/quant-cover.png", "assets/quant-page-55.png"):
    if not (ROOT / path).is_file():
        continue
    uri = data_uri(path, "image/png")
    html = html.replace(path, uri)
    css = css.replace(path, uri)

bundles = {
    "pdfjs": "vendor/pdf.min.mjs",
    "pdfworker": "vendor/pdf.worker.min.mjs",
    "fflate": "vendor/fflate.mjs",
}
encoded = {name: base64.b64encode((ROOT / path).read_bytes()).decode() for name, path in bundles.items()}
import json

bootstrap = "const DEMO_MODULES=" + json.dumps(encoded, separators=(",", ":")) + ";\n" + """
const demoModuleURLs = new Map();
function moduleURL(name) {
  if (!demoModuleURLs.has(name)) {
    const bytes = Uint8Array.from(atob(DEMO_MODULES[name]), c => c.charCodeAt(0));
    demoModuleURLs.set(name, URL.createObjectURL(new Blob([bytes], {type:'text/javascript'})));
  }
  return demoModuleURLs.get(name);
}
"""
source = ROOT / "打开量化投资的黑箱.pdf"
if source.is_file():
    bootstrap += "globalThis.YEJIAN_PDF_BASE64=" + json.dumps(base64.b64encode(source.read_bytes()).decode()) + ";\n"
else:
    bootstrap += "globalThis.PAGEASK_TEXT_SAMPLE=" + json.dumps((ROOT / "examples/reading-practice.txt").read_text(), ensure_ascii=False) + ";\n"
js = js.replace("import('./vendor/pdf.min.mjs')", "import(moduleURL('pdfjs'))")
js = js.replace("import('./vendor/fflate.mjs')", "import(moduleURL('fflate'))")
js = js.replace("new URL('./vendor/pdf.worker.min.mjs',document.baseURI).href", "moduleURL('pdfworker')")

# Prevent text content from accidentally terminating an inline script element.
script = re.sub(r"</script", r"<\\/script", bootstrap + js, flags=re.IGNORECASE)
html = html.replace('<link rel="stylesheet" href="styles.css">', "<style>\n" + css + "\n</style>")
html = html.replace('<script src="ai-reader.js" defer></script>', "")
html = html.replace('<script src="ask-reader.js" defer></script>', "")
html = html.replace('<script src="pdf-reader.js" defer></script>', "")
html = html.replace('<script src="app.js" defer></script>', "")
html = html.replace('<script src="content.js" defer></script>', "")
html = html.replace("</body>", "<script>\n" + script + "\n</script>\n</body>")
(ROOT / "demo.html").write_text(html)
print(f"Built demo.html ({len(html.encode()):,} bytes)")
