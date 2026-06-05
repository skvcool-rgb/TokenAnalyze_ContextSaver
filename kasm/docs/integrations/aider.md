# Aider

Aider doesn't speak MCP, but it reads files passed via `--read`
or whose content is piped in via `--message-file`. Use the kos-memory
HTTP server to fetch state and feed Aider as a one-shot context
preamble.

## Step 1: Start the server

```bash
python -m mcp.http_server --port 7621 --token mysecret
```

Or use the slash command from Claude Code: `/memory-serve --token mysecret`.

## Step 2: Fetch context, feed Aider

```bash
TOKEN=mysecret
PROJECT="$(pwd)"

# Fetch live state + recent recall passages
curl -s -X POST http://127.0.0.1:7621/v1/recall \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"current sprint\",\"window_days\":7}" \
  > /tmp/kos-recall.json

# Strip JSON envelope to get just the catalog + passages
python -c "
import json, sys
d = json.load(open('/tmp/kos-recall.json'))
print('# Recent project memory\\n')
print(d['data']['catalog_text'])
print('\\n## Passages\\n')
for p in d['data']['passages']:
    print(p['text'])
    print('---')
" > /tmp/kos-context.md

# Pass to Aider
aider --read /tmp/kos-context.md src/your-file.py
```

## Step 3: After Aider

When Aider commits, push the commit message back as a remembered fact:

```bash
LAST_MSG="$(git log -1 --pretty=%B)"

curl -s -X POST http://127.0.0.1:7621/v1/remember \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg fact "$LAST_MSG" '{fact: $fact, tags: ["aider", "commit"]}')"
```

## Wrapping with a shell function

```bash
aider-with-memory() {
  curl -s http://127.0.0.1:7621/v1/state \
    -H "Authorization: Bearer ${KOS_MEMORY_TOKEN}" \
    | python -c 'import json,sys; print(json.load(sys.stdin)["data"]["reconciliation_text"])' \
    > /tmp/aider-context.md
  aider --read /tmp/aider-context.md "$@"
}
```

## Easy mode

Run `python scripts/install.py` for the Claude Code side. The Aider
side is shell-wrapper-only — copy the function above into your
`.bashrc` / `.zshrc`.
