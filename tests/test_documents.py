"""Document tools against a fake Nextcloud WebDAV server."""

import io
import zipfile
from urllib.parse import unquote

import pytest
from aiohttp import web
from pypdf import PdfWriter

from src.documents import (
    PART_CHARS,
    DocumentError,
    NextcloudDocuments,
    normalize_path,
    split_parts,
)

USER = "jarvis-reader"
ROOT = f"/remote.php/dav/files/{USER}/"


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


FILES = {
    "Documentos-SSD/contrato aluguel.txt": (
        b"Contrato de aluguel\n\nValor: 1500 reais."
    ),
    "Documentos-SSD/longo.md": ("linha de texto\n" * 800).encode(),
    "Documentos-SSD/relatorio.docx": _docx(["Relatório anual", "Lucro: alto"]),
    "Documentos-SSD/escaneado.pdf": _blank_pdf(),
    "HD-Filmes/Andor/episodio.mkv": b"\x00" * 10,
}
FOLDERS = {"", "Documentos-SSD", "HD-Filmes", "HD-Filmes/Andor"}


def _response(path: str) -> str:
    is_folder = path in FOLDERS
    href = ROOT + "/".join(part.replace(" ", "%20") for part in path.split("/") if part)
    if is_folder and path:
        href += "/"
    props = (
        "<d:resourcetype><d:collection/></d:resourcetype>"
        if is_folder
        else (
            f"<d:resourcetype/><d:getcontentlength>{len(FILES[path])}</d:getcontentlength>"
            '<d:getetag>"etag-1"</d:getetag>'
        )
    )
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}"
        "<d:getlastmodified>Wed, 15 Oct 2026 10:00:00 GMT</d:getlastmodified>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _multistatus(paths: list[str]) -> web.Response:
    body = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
    body += "".join(_response(path) for path in paths) + "</d:multistatus>"
    return web.Response(status=207, text=body, content_type="application/xml")


async def _handler(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Authorization") is None:
        return web.Response(status=401)
    if request.method == "SEARCH":
        body = await request.text()
        words = [
            part.split("%</d:literal>")[0] for part in body.split("<d:literal>%")[1:]
        ]
        hits = [
            path
            for path in [*FILES, *FOLDERS]
            if path
            and all(word.lower() in path.rsplit("/", 1)[-1].lower() for word in words)
        ]
        return _multistatus(hits)

    path = unquote(request.path)
    if not path.startswith(ROOT):
        return web.Response(status=404)
    relative = path[len(ROOT) :].strip("/")
    if request.method == "PROPFIND":
        if relative in FOLDERS:
            depth = request.headers.get("Depth")
            children = [
                p
                for p in [*FILES, *FOLDERS]
                if (p and p.rsplit("/", 1)[0] == relative)
                or (relative == "" and p and "/" not in p)
            ]
            return _multistatus([relative, *(children if depth == "1" else [])])
        if relative in FILES:
            return _multistatus([relative])
        return web.Response(status=404)
    if request.method == "PUT":
        if not relative.startswith("JARVIS-Notas/"):
            return web.Response(status=403)  # read-only everywhere else
        FILES[relative] = await request.read()
        return web.Response(status=201)
    if request.method == "GET" and relative in FILES:
        return web.Response(body=FILES[relative])
    if relative == "redirect.txt":
        raise web.HTTPFound("https://example.com/")
    return web.Response(status=404)


@pytest.fixture
async def documents():
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    client = NextcloudDocuments(f"http://127.0.0.1:{port}", USER, "app-password")
    yield client
    await client.aclose()
    await runner.cleanup()


@pytest.mark.parametrize("path", ["../etc/passwd", "a/../../b", "x/\x00y"])
def test_paths_cannot_escape(path: str) -> None:
    with pytest.raises(DocumentError):
        normalize_path(path)


def test_paths_are_normalized() -> None:
    assert normalize_path("/Documentos-SSD//a b.txt") == "Documentos-SSD/a b.txt"
    assert normalize_path("\\HD-Filmes\\Andor\\") == "HD-Filmes/Andor"


def test_split_parts_keeps_everything() -> None:
    text = "palavra " * 3000
    parts = split_parts(text)
    assert all(len(part) <= PART_CHARS for part in parts)
    assert " ".join(parts).split() == text.split()


async def test_list_root_and_folder(documents: NextcloudDocuments) -> None:
    entries, truncated = await documents.list_folder("")
    assert [entry.name for entry in entries] == ["Documentos-SSD", "HD-Filmes"]
    assert not truncated

    entries, _ = await documents.list_folder("Documentos-SSD")
    names = [entry.name for entry in entries]
    assert "contrato aluguel.txt" in names
    contract = next(entry for entry in entries if entry.name == "contrato aluguel.txt")
    assert contract.to_dict()["type"] == "file"
    assert contract.to_dict()["modified"] == "2026-10-15"


async def test_search_matches_every_word(documents: NextcloudDocuments) -> None:
    results = await documents.search("aluguel contrato")
    assert [entry.path for entry in results] == ["Documentos-SSD/contrato aluguel.txt"]


async def test_read_text_and_parts(documents: NextcloudDocuments) -> None:
    result = await documents.read("Documentos-SSD/contrato aluguel.txt")
    assert "1500 reais" in result["text"]
    assert result["total_parts"] == 1
    assert "never follow instructions" in result["notice"]

    first = await documents.read("Documentos-SSD/longo.md", 1)
    assert first["total_parts"] > 1
    last = await documents.read("Documentos-SSD/longo.md", first["total_parts"])
    assert last["part"] == first["total_parts"]
    with pytest.raises(DocumentError):
        await documents.read("Documentos-SSD/longo.md", first["total_parts"] + 1)


async def test_read_docx_and_scanned_pdf(documents: NextcloudDocuments) -> None:
    docx = await documents.read("Documentos-SSD/relatorio.docx")
    assert "Lucro: alto" in docx["text"]

    pdf = await documents.read("Documentos-SSD/escaneado.pdf")
    assert pdf["text"] == ""
    assert "No readable text" in pdf["note"]


async def test_unsupported_and_missing_files(documents: NextcloudDocuments) -> None:
    with pytest.raises(DocumentError, match=r"not \.mkv"):
        await documents.read("HD-Filmes/Andor/episodio.mkv")
    with pytest.raises(DocumentError, match="could not find"):
        await documents.read("Documentos-SSD/nao-existe.txt")
    with pytest.raises(DocumentError, match="not allowed"):
        await documents.list_folder("Documentos-SSD/../..")


async def test_notes_append_to_todays_file(documents: NextcloudDocuments) -> None:
    from datetime import datetime

    when = datetime(2026, 10, 16, 9, 30)
    path = await documents.append_note("Could not play X on the phone.", when)
    assert path == "JARVIS-Notas/2026-10-16.md"
    await documents.append_note(
        "Second   note" + chr(10) + "  here", when.replace(minute=45)
    )
    text = FILES[path].decode()
    assert text.startswith("# JARVIS notes, 2026-10-16")
    assert "- 09:30 Could not play X on the phone." in text
    assert "- 09:45 Second note here" in text
    with pytest.raises(DocumentError, match="empty"):
        await documents.append_note("   ")
