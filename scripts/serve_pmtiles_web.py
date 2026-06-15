from __future__ import annotations

import argparse
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class RangeRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        if path.endswith(".pmtiles"):
            return "application/octet-stream"
        return super().guess_type(path)

    def send_head(self):
        path = self.translate_path(self.path)
        file_path = Path(path)

        if file_path.is_dir():
            return super().send_head()

        if not file_path.exists():
            self.send_error(404, "File not found")
            return None

        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        try:
            units, range_spec = range_header.split("=", 1)
            if units.strip() != "bytes":
                raise ValueError("Only bytes range is supported")

            start_text, end_text = range_spec.split("-", 1)
            file_size = file_path.stat().st_size
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else file_size - 1
            end = min(end, file_size - 1)

            if start < 0 or end < start or start >= file_size:
                self.send_error(416, "Requested Range Not Satisfiable")
                return None
        except Exception:
            self.send_error(400, "Bad Range header")
            return None

        content_type = self.guess_type(str(file_path))
        f = file_path.open("rb")
        f.seek(start)

        self.range = (start, end)
        self.send_response(206)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(file_path.stat().st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile) -> None:
        if hasattr(self, "range"):
            start, end = self.range
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
            del self.range
            return

        super().copyfile(source, outputfile)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("Taipei_City_Urban_Resilience_Map_Website") / "road_pmtiles_web",
    )
    args = parser.parse_args()

    web_dir = args.directory.resolve()
    if not web_dir.exists():
        raise FileNotFoundError(web_dir)

    mimetypes.add_type("application/octet-stream", ".pmtiles")

    handler = lambda *a, **kw: RangeRequestHandler(
        *a,
        directory=str(web_dir),
        **kw,
    )

    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {web_dir} at http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
