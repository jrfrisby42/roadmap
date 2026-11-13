"""DESC-CHECKBOX-1 (checkbox lists in descriptions) + TMPL-BOX-1 (Admin type-template editor box).

Server: the public render sanitizer (portal /ticket page + emails) shows a stored Tiptap checkbox list as
un-bulleted lines prefixed with a box glyph, without letting any stored attribute through.
Client: guards on the wiring (loader, extensions, both sanitizer allow-lists, toolbar sets, Jira ADF).
The editor behaviour itself was verified headlessly against the real app.
"""
import pathlib
import re

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")

TASK = ('<ul data-type="taskList">'
        '<li data-type="taskItem" data-checked="true"><div><p>Windows</p></div></li>'
        '<li data-type="taskItem" data-checked="false"><div><p>Mac</p></div></li>'
        '</ul>')


def test_public_render_prefixes_box_glyphs():
    out = server._sanitize_html("<p><strong>Laptop:</strong></p>" + TASK)
    assert "☑ Windows" in out
    assert "☐ Mac" in out
    assert '<ul style="list-style:none;padding-left:4px">' in out


def test_public_render_drops_stored_attributes():
    out = server._sanitize_html(TASK)
    assert "data-checked" not in out and "data-type" not in out


def test_public_render_glyph_lands_on_first_text_not_before_block():
    # the glyph must sit on the item's text line, not on its own line ahead of the <div><p>
    out = server._sanitize_html(TASK)
    assert "<li><div><p>☑ Windows</p></div></li>" in out


def test_public_render_empty_task_item_still_shows_box():
    out = server._sanitize_html('<ul data-type="taskList"><li data-type="taskItem" data-checked="false"><div><p></p></div></li></ul>')
    assert "☐ </li>" in out


def test_public_render_plain_lists_unchanged():
    src = "<ul><li>one</li></ul><ol><li>two</li></ol>"
    assert server._sanitize_html(src) == src


def test_public_render_still_strips_handlers_inside_task_items():
    out = server._sanitize_html('<ul data-type="taskList"><li data-type="taskItem" data-checked="true" onclick="x()">'
                                '<div><p><img src=x onerror=alert(1)>A</p></div></li></ul>')
    assert "onclick" not in out and "onerror" not in out and "<img" not in out
    assert "☑ A" in out


def test_client_loads_task_extensions_at_pinned_version():
    assert "import(b+'extension-task-list@'+V)" in HTML
    assert "import(b+'extension-task-item@'+V)" in HTML
    assert "T.TaskList, T.TaskItem.configure({ nested:false })" in HTML


def test_client_sanitizers_keep_data_checked():
    cfg = HTML.split("var _FRZ_SANI_CFG={", 1)[1].split("};", 1)[0]
    assert "'data-checked'" in cfg, "editor save sanitizer must keep data-checked"
    srh = HTML.split("var _SRH_SAFE_ATTRS = {", 1)[1].split("};", 1)[0]
    assert "'data-checked':1" in srh, "render sanitizer must keep data-checked (the modal loads #fDesc through it)"
    # form controls stay out of stored HTML
    assert "'input'" not in cfg.split("ALLOWED_TAGS:", 1)[1].split("]", 1)[0]


def test_client_toolbar_sets():
    def tb(name):
        return HTML.split("var " + name + "=", 1)[1].split(";", 1)[0]
    for name in ("_FRZ_TB_CREATE", "_FRZ_TB_MODAL", "_FRZ_TB_FULL"):
        assert "'task'" in tb(name), f"{name} must offer the checkbox list"
    assert "'task'" not in tb("_FRZ_TB_COMMENT"), "comments keep their trimmed set"
    assert "buttons:_FRZ_TB_MODAL" in HTML, "the edit modal description uses the modal set"


def test_client_jira_adf_glyphs():
    fn = HTML.split("function htmlToADF(html){", 1)[1].split("\nfunction ", 1)[0]
    assert "data-type')==='taskList'" in fn
    assert "'☑ '" in fn and "'☐ '" in fn


def test_tmpl_box_styles_present():
    assert "body.frz-beta-active .type-tmpl-host .frz-rte-modal { border:1px solid var(--border)" in HTML
    assert 'class="tmpl-save primary"' in HTML
    assert not re.search(r"#5b4fff|#7b6fff|#4a3de0", HTML.split("TMPL-BOX-1", 1)[1].split("</style>", 1)[0])
