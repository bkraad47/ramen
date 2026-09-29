"""0.5.4: the group page's environment rows use the equal-width row-actions pattern (0.4.2 W4) for all three
buttons — the verbose toggle, Deploy (canary) and Delete were three sizes on two baselines."""

import re

from tests.test_api import app, client, cloud, demo, root  # noqa: F401 - pytest fixtures


def test_environment_row_buttons_are_equal_width_and_on_one_line(demo):
    page = demo.get("/groups/demo").text
    rows = re.findall(
        r"<tr>(.*?)</tr>", page[page.index("<h2>Environments</h2>") : page.index("<h2>Deploy jobs</h2>")], re.S
    )
    env_rows = [r for r in rows if "Deploy (canary)" in r]
    assert env_rows, "the seeded group has environments"
    for row in env_rows:
        buttons = re.findall(r"<button[^>]*>", row)
        assert len(buttons) == 3, buttons
        for b in buttons:
            assert 'class="act' in b or 'class="act' in b or " act " in b or ' act"' in b, b
        assert row.count('class="row-actions"') == 2, "verbose toggle and the actions each sit in a row-actions cell"
