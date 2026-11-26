import zipfile

from conftest import sh


def test_pandoc_converts_markdown_inside_the_sandbox(make_sandbox):
    ctl, sbx = make_sandbox()
    (sbx.workdir / "in.md").write_text("# Title\n\nHello **world**.\n")
    r = sh(ctl, sbx, "pandoc in.md -o out.html && pandoc in.md -o out.docx")
    assert r.exit_code == 0, r.stderr
    assert "<strong>world</strong>" in (sbx.workdir / "out.html").read_text()
    with zipfile.ZipFile(sbx.workdir / "out.docx") as z:      # a .docx is a zip with word/document.xml
        assert "world" in z.read("word/document.xml").decode()
