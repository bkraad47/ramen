"""1.0.0c D84: the group page's Evals, Scans and Guardrails cards share one control row (`policy_card` macro): Required
switch · Gate switch · LLM provider select · Model select · Save, the status badges on the right."""

import asyncio

import pytest

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_v100c_ui_providers_regions import GROUP, KEY, ORG, _fresh_cache, provider  # noqa: F401 - fixtures

KINDS = ("evals", "scans", "guardrails")
CONTROLS = ("pol-required", "pol-llm", "pol-model", "pol-save", "pol-status")


def card(page: str, kind: str) -> str:
    return page.split(f'<div class="card policy" id="policy-{kind}">')[1].split("</form>")[0]


def options(select_html: str) -> list[str]:
    return [o.split('"', 1)[0] for o in select_html.split('<option value="')[1:]]


def select(html: str, name: str) -> str:
    return html.split(f'<select name="{name}"')[1].split("</select>")[0]


def test_the_three_cards_render_one_control_row_with_the_same_classes(demo):  # noqa: F811
    page = demo.get("/groups/demo").text
    for kind in KINDS:
        c = card(page, kind)
        assert '<form class="policy-row"' in c and c.count("<form") == 1, kind
        for cls in CONTROLS:
            assert f'class="{cls}' in c or f" {cls}" in c, (kind, cls)
        assert 'name="required" value="true" data-bool' in c and f'id="{kind}-model"' in c, kind
        assert '<select name="model"' in c and '<select name="llm"' in c, kind
        assert "pol-save" in c.split("</select>")[-1], "Save is last in the row"
    assert "pol-gate" in card(page, "evals") and "pol-gate" in card(page, "scans")
    assert "pol-gate" not in card(page, "guardrails")  # guardrails have no deploy gate (§36.3)
    assert 'hx-put="/api/v1/groups/demo/evals"' in card(page, "evals")
    assert 'hx-put="/api/v1/groups/demo/skillspector"' in card(page, "scans")
    assert 'hx-put="/api/v1/groups/demo/guardrails"' in card(page, "guardrails")
    assert "-32029" in card(page, "guardrails") and "signs in again" in card(page, "guardrails")
    assert 'name="llm" value=' not in page  # the free-text scan field is gone
    assert "form.policy-row" in page and "data-bool" in page  # the switches post true / false


def test_the_selects_list_the_resolved_providers_group_first_then_org_and_none_first_for_evals_and_scans(demo):  # noqa: F811
    page = demo.get("/groups/demo").text
    assert '<option value="">none (static rules only)</option>' in select(card(page, "scans"), "llm")
    assert "none" in select(card(page, "evals"), "llm") and "disabled" in card(page, "evals").split("pol-llm")[1]
    assert '<option value="">no provider configured yet</option>' in card(page, "evals")
    demo.put(ORG, json={"openai": {"api_key": KEY, "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    demo.put(ORG, json={"anthropic": {"api_key": KEY}, "vertex": {"project": "p", "location": "us-central1"}})
    demo.put(GROUP, json={"openai_compatible": {"api_key": KEY, "base_url": "https://llm.example/v1"}})
    demo.put(GROUP, json={"openai_compatible": {"allowed_models": ["llama-3"]}})
    page = demo.get("/groups/demo").text
    scans, rails, evals = card(page, "scans"), card(page, "guardrails"), card(page, "evals")
    assert options(select(scans, "llm")) == ["", "openai_compatible", "anthropic", "openai"]  # no vertex (D73)
    assert options(select(rails, "llm")) == ["", "openai_compatible", "anthropic", "openai", "vertex"]
    assert '<option value="">first configured (openai_compatible)</option>' in rails
    assert 'title="group scope"' in rails and 'title="org scope"' in rails
    # evals: no setting of their own (§29.2) — the resolved scope, read-only in the same slot
    assert "<option>group: openai_compatible</option>" in evals and "disabled" in select(evals, "model")
    assert '<option value="">default: llama-3</option>' in select(evals, "model")
    # the model slot follows the chosen provider (re-fetched on change like the guardrails card)
    assert 'hx-get="/ui/groups/demo/guardrails/models?kind=scans"' in scans and 'hx-target="#scans-model"' in scans
    assert 'hx-get="/ui/groups/demo/guardrails/models?kind=guardrails"' in rails
    part = demo.get("/ui/groups/demo/guardrails/models?kind=scans&llm=openai").text
    assert "disabled" in part and "default: gpt-4o-mini" in part and "openai · org scope" in part
    assert "static rules only" in demo.get("/ui/groups/demo/guardrails/models?kind=scans&llm=").text
    part = demo.get("/ui/groups/demo/guardrails/models?kind=guardrails&llm=openai").text
    assert "disabled" not in part and '<option value="gpt-4o">gpt-4o</option>' in part
    assert demo.get("/ui/groups/demo/guardrails/models?kind=nope").status_code == 422


def test_the_rows_post_to_the_existing_routes_and_the_page_shows_what_was_saved(demo):  # noqa: F811
    demo.put(ORG, json={"openai": {"api_key": KEY, "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    # what the switches and selects send (the json extension posts "true" / "false" strings)
    assert demo.put("/api/v1/groups/demo/evals", json={"required": "true", "gate": "false"}).status_code == 200
    assert demo.put("/api/v1/groups/demo/skillspector", json={"gate": "true", "llm": "openai"}).status_code == 200
    r = demo.put("/api/v1/groups/demo/guardrails", json={"required": "true", "llm": "openai", "model": "gpt-4o"})
    assert r.status_code == 200
    page = demo.get("/groups/demo").text
    assert 'name="required" value="true" data-bool checked' in card(page, "evals")
    assert 'name="gate" value="true" data-bool checked' in card(page, "scans")
    assert 'name="gate" value="true" data-bool' in card(page, "evals")
    assert 'name="gate" value="true" data-bool checked' not in card(page, "evals")
    assert '<option value="openai" title="org scope" selected>' in select(card(page, "scans"), "llm")
    assert '<option value="gpt-4o" selected>gpt-4o</option>' in select(card(page, "guardrails"), "model")
    assert 'name="required" value="true" data-bool checked' in card(page, "guardrails")
    g = demo.get("/api/v1/groups/demo").json()
    assert g["evals"] == {"gate": False, "required": True} and g["skillspector"]["llm"] == "openai"
    assert demo.put("/api/v1/groups/demo/skillspector", json={"llm": "vertex"}).status_code == 422


def test_the_status_badges_show_the_last_run_and_the_policy_version(demo):  # noqa: F811
    store = demo.app.state.store
    env = {
        "id": "demo:prod",
        "group": "demo",
        "name": "prod",
        "ref": "main",
        "zones": ["zone-a"],
        "last_deploy": {
            "status": "ok",
            "at": "2026-11-16T00:00:00Z",
            "job": "j1",
            "evals": {
                "zone-a": {"passed": 3, "failed": 1, "errors": 0, "url": "/groups/demo/evals/j1/zone-a"},
                # a record from before the run page: promptfoo's in-cluster share link is never offered
                "zone-b": {"passed": 1, "failed": 0, "errors": 0, "url": "https://promptfoo.ramen-system:3000/eval/x"},
            },
            "skillspector": {"zone-a": {"recommendation": "SAFE", "score": 7, "skills": 2, "findings": 1, "url": "/x"}},
        },
    }
    asyncio.run(store.put("environments", env["id"], env))
    page = demo.get("/groups/demo").text
    ev, sc, gr = (card(page, k).split("pol-status")[1] for k in KINDS)
    assert (
        "evals: 1 failed" in ev
        and 'href="/groups/demo/evals/j1/zone-a"' in ev
        and "promptfoo.ramen-system" not in ev
        and "prod/zone-a" in ev
    )
    assert "scan: SAFE" in sc and 'href="/x"' in sc
    assert "no mcp/guardrails.yaml" in gr and "no mcp/evals/promptfooconfig.yaml" in ev
    assert "policy" in sc and ("off" in sc or "version" in sc)


@pytest.mark.parametrize("who, role", [("v@x", "viewer"), ("o@x", "group_admin")])
def test_the_cards_are_the_group_admins(demo, who, role):  # noqa: F811
    from fastapi.testclient import TestClient

    make_user(demo, who, role, ["demo" if role == "viewer" else "other"])
    with TestClient(demo.app) as u:
        login(u, who, PW)
        r = u.get("/groups/demo")
        if role == "viewer":
            assert r.status_code == 200 and "policy-row" not in r.text
        else:
            assert r.status_code == 403
        assert u.get("/ui/groups/demo/guardrails/models?kind=scans").status_code == 403
