"""COPY-LINK-1: one shared copy-link action on the List rows, the List panel, the modal and the item page.

Frontend-only; these are source guards (the behaviour was verified headlessly when it was built)."""
import pathlib

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_shared_helper_copies_the_canonical_item_url():
    assert "function copyItemLink(id, el){" in HTML
    body = HTML.split("function copyItemLink(id, el){", 1)[1].split("\n}\n", 1)[0]
    assert "_frzItemPageUrl(id)" in body            # /item/{id}?team= - same URL as every new-tab control
    assert "'text/html'" in body and "'text/plain'" in body   # titled link + bare URL


def test_all_four_surfaces_use_it():
    assert "${_listCell(it.itemKey)}${copyLinkBtnHtml(it.id)}" in HTML                # List row (Key cell)
    assert "_call('copyLinkBtnHtml', p.id, 'frz-lp-copy')" in HTML                    # List side panel
    assert "_keyLink+copyLinkBtnHtml(p.id)" in HTML                                    # item modal header
    assert 'id="frzCopyLinkBtn"' in HTML and "_call('copyItemLink', id, clb)" in HTML  # item page
    assert "function ipCopyLink(){ copyItemLink(_itemPageId); }" in HTML               # classic menu entry


def test_row_click_handlers_ignore_the_copy_button():
    # both the classic bubble handler and the Flow panel capture handler must skip it, or a copy opens the item
    assert HTML.count("closest('.copy-link-btn')) return;") == 2
