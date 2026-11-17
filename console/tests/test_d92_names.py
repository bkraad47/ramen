"""D92: a group or zone name becomes a DNS label in the zone's TLS name (`<group>.<zone>.worker.ramen.internal`), so it
must end with a letter or digit — a trailing `-` is not a valid label for rustls (or Kubernetes)."""

import pytest

from ramen_console.util import NAME_RE


@pytest.mark.parametrize("name", ["demo", "a", "a2", "my-group", "x" * 40, "a-b-c"])
def test_valid_names(name):
    assert NAME_RE.match(name)


@pytest.mark.parametrize("name", ["demo-", "a-", "-a", "2a", "Demo", "a_b", "x" * 41, ""])
def test_invalid_names(name):
    assert not NAME_RE.match(name)
