"""The topo generator as a Vercel function, at /api/model.

Vercel's api/ directory is file-based: this file answers /api/model, which is
the path the page calls whether it is talking to Vercel or to src/app.py.  It
runs the BaseHTTPRequestHandler subclass it finds *defined* here under the
name `handler` -- that look is static, so importing src/app.py's Handler
under the name is not enough; the class has to be written out.  The page
itself is public/index.html, served as a static file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app import Handler  # noqa: E402


class handler(Handler):
    """src/app.py's request handler, defined here so Vercel can see it."""
