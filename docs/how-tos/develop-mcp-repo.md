# Write and develop an MCP repo (with the demo)

A group's tools live in a plain git repo with
one folder per package; the [demo repo](https://github.com/bkraad47/ramen-demo-mcp-group) is the smallest complete
example and the one every guide deploys.

## The layout
```
mcp/
  requirements.txt                  pip requirements the worker installs on load
  env.yaml                          environment variables for your code, rendered from the group's secrets (.env also works)
  tools/<name>/<name>.py            the callable
  tools/<name>/<name>.json          the proto: type, name, description, callable, input, output, error
  resources/<name>/<name>.py+.json  the same shape plus uri and mime_type
  prompts/<name>/<name>.json        plus SKILL.md (the agent-skills template) and settings.json
```
The worker enforces three rules. The folder name, the `name` in the JSON and the Python file's stem must all
match. `type` must match the parent folder. `input` maps each parameter to a JSON schema, and every parameter is
required unless its schema says otherwise. The whole contract is in [Protos](../wiki/protos.md).

```json title="mcp/tools/demo_calculator_tool/demo_calculator_tool.json"
{"type": "tool", "name": "demo_calculator_tool", "description": "Add, subtract, multiply or divide two numbers.",
 "callable": "calculator_func",
 "input": {"var1": {"type": "number"}, "var2": {"type": "number"}, "func": {"enum": ["add", "subtract", "multiply", "divide"]}},
 "output": {"type": "number"}, "error": {"type": "string"}}
```
```python title="mcp/tools/demo_calculator_tool/demo_calculator_tool.py"
def calculator_func(var1: float, var2: float, func: str) -> float:
    return {"add": var1 + var2, "subtract": var1 - var2, "multiply": var1 * var2, "divide": var1 / var2}[func]
```

## Secrets and the environment
Two ways to reach a secret, both rendered by the worker from the secrets set on the console (never from the repo):

- In a **call argument**: `{{$demo.OPENAI_API_KEY}}` inside any string the client passes is substituted before your
  function runs ([Secrets](secrets.md)).
- In **`mcp/env.yaml`** (0.6.0): flat `KEY: value` lines the worker exports to the runtime process at load, with
  `{{$group.SECRET}}` references rendered. Tool code reads `os.environ["DB_URL"]`. A reference with no secret behind
  it is skipped with a warning (the rest of the file still applies); rendered values are redacted from error
  messages and never logged.
  ```yaml title="mcp/env.yaml"
  DEMO_MODE: demo
  DB_URL: "postgres://app:{{$demo.DB_PASSWORD}}@db.internal/app"
  ```

Cloud access needs no keys at all: the workers of a zone run as that zone's service account / IAM role, with
exactly the permissions approved on the group page ([Service accounts and secrets](service-accounts.md)). A tool
that calls `google.cloud.storage` or `boto3` picks that identity up automatically.

## Develop locally
1. `git clone https://github.com/bkraad47/ramen && cd ramen && make up` — the compose stack: console
   `https://localhost:8443` (admin@ramen.local / changeme-ramen), one worker at `localhost:8080`.
2. Create a group pointing at **your** repo (or fork the demo), attach zone `local` to an environment, deploy. The
   deploy job streams the clone, the upload, the worker reload and the packages each zone reported.
3. Call it: `make demo` does the whole loop for the demo repo; for your own, use the key from the group page with
   `curl`, the `mcp` SDK or the bridge ([Connect an MCP client](mcp-clients.md)).
4. Iterate: push to the repo, deploy again. Blocked packages, verbose logging and the per-zone list are on the
   group page; the *Logs* page tails the worker.

<figure markdown>
![A deploy job: clone, upload, canary, reload, smoke](../img/deploy-job.png){ .ramen-shot }
</figure>

Running the runtime by itself (no console, no node) is the quickest way to test a change:
```sh
cd runtime-py && uv sync --all-extras          # from the ramen checkout
RAMEN_SECRET_DEMO__DB_PASSWORD=x uv run python -m ramen_runtime --bucket /path/to/your/repo --once tools/call demo_calculator_tool '{"var1":2,"var2":3,"func":"add"}'
```
and `uv run pytest` in `runtime-py/` runs the proto validator against the demo fixtures.

## Ship it
- **Requirements**: pin them; the worker pip-installs `mcp/requirements.txt` into its own environment on every load
  and reports a dependency error on the deploy job instead of a half-loaded group.
- **Heavy stacks** (numpy, pandas, a CUDA wheel): record a worker image for the group instead of installing on every
  load ([pin a worker image](devops-api.md#pin-a-worker-image-per-group)).
- **Private repos**: a `GITHUB_TOKEN` secret on the group, or a GitHub App installation id on the group with the
  App configured on the Config page — the console clones, workers never do.
- **Versions**: an environment pins a git ref, so `dev` can track `main` while `prod` sits on a tag; rolling back is
  deploying the older ref.
