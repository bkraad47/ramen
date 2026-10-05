# Vendored front-end files

| File | Project | Version | Source | sha256 |
|---|---|---|---|---|
| `htmx.js` | htmx (BSD Zero Clause) | 2.0.4 | https://github.com/bigskysoftware/htmx/releases/download/v2.0.4/htmx.js | `cb0a99bf91c36bdd39e0c9d4677c579e8202f9a58db5f0c59c90085ea0e41275` |

`htmx.js` is the unminified build shipped in the v2.0.4 GitHub release; the same bytes are served by
`https://unpkg.com/htmx.org@2.0.4/dist/htmx.js` and `https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.js`
(all three downloads were compared on 2026-10-05). It replaced `htmx.min.js` of the same version in 0.7.0 so the
code the browser runs can be read and audited (R4). Verify with `shasum -a 256 htmx.js`; `tests/test_vendor.py`
checks the file against this table.

Not vendored, written here: `ramen.css`, `logo*.png`, `favicon.png`.
