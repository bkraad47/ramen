# ramen-demo-mcp-group

A ready-to-connect **group repo** for [Ramen](https://github.com/bkraad47/ramen). Point a Ramen group at this repo, click Deploy, and you get one tool, one resource and one prompt.

## Layout (the Ramen group contract)
```
mcp/
  requirements.txt                       pip requirements for the worker
  tools/<name>/<name>.py                 callable code (utils/ is importable)
  tools/<name>/<name>.json               proto: type, name, callable, input, output, error
  resources/<name>/<name>.py + .json     same shape, plus uri and mime_type
  prompts/<name>/<name>.json             + SKILL.md (agent-skills template) + settings.json
```
Rules: folder name == json `name` == file stem; json `type` must match its folder; `type` ∈ {tool, resource, prompt}.
Secrets are referenced in code as `{{$group_name.secret_var}}` and resolved by the worker at call time.

## Try it
1. Deploy Ramen (see its README).
2. Create a group, set its repo to `https://github.com/bkraad47/ramen-demo-mcp-group`.
3. Deploy, then call `demo_calculator_tool` with `{"var1": 2, "var2": 3, "func": "add"}` from any MCP client.
