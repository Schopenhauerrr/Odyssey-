"""Build docs/index.html (the installable app) from app/odyssey.html.
Run after changing the app source:  python tools/build_app.py"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "app" / "odyssey.html").read_text(encoding="utf-8")
data = ROOT / "docs" / "data"
for name, ph in (("settings", "/*SETTINGS*/"), ("refs", "/*REFS*/"), ("deals", "/*DEALS*/")):
    src = src.replace(ph, (data / f"{name}.json").read_text(encoding="utf-8").replace("</", "<\\/"))

head, body = src.split("<!--BODY-->", 1)
page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icons/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Odyssey">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
{head.strip()}
</head>
<body>
{body.strip()}
</body>
</html>
"""
(ROOT / "docs" / "index.html").write_text(page, encoding="utf-8")
if "--preview" in sys.argv:  # fragment for the claude.ai preview
    (ROOT / "app" / "preview.html").write_text(head.strip() + "\n" + body.strip() + "\n", encoding="utf-8")
print("built docs/index.html")
