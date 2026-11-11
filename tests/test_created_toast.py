"""CREATED-TOAST-1: after a new item saves (and after the Jira prompt), a toast says "Item KEY created" with
the key linking to the item page. Frontend-only; source guards (verified headlessly when built)."""
import pathlib

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_toast_links_the_key_to_the_item_page():
    body = HTML.split("function showItemCreatedToast(p, jiraKey){", 1)[1].split("\n}\n", 1)[0]
    assert "_frzItemPageUrl(p.id)" in body and "p.itemKey" in body and "jiraKey" in body


def test_create_flow_fires_it_after_the_save_settles():
    assert "_createdToast = { id: created.id, jira: '' };" in HTML
    assert "const _jk = await createJiraIssueFromModalForItem(created);" in HTML
    after = HTML.split("await _afterItemSave(savedProject, _originalItemValues);", 1)[1][:400]
    assert "showItemCreatedToast(" in after          # last in the save tail, so nothing overwrites it


def test_jira_helper_returns_the_new_key():
    assert "return result.key;   // CREATED-TOAST-1" in HTML
