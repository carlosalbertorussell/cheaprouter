# Connecting to cheaprouter

cheaprouter is deployed on MCPize and can be used from any MCP-compatible client.
It is BYOK — you pass your own provider API keys with each request.

## Claude Desktop (remote)

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cheaprouter": {
      "type": "http",
      "url": "https://<your-cheaprouter-endpoint>.mcpize.run/mcp"
    }
  }
}
```

## Claude Desktop (local, stdio)

Clone the repo and run locally:

```json
{
  "mcpServers": {
    "cheaprouter": {
      "command": "python",
      "args": ["/path/to/cheaprouter/server.py", "--stdio"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "GROQ_API_KEY": "gsk_...",
        "DEEPSEEK_API_KEY": "sk-..."
      }
    }
  }
}
```

In local stdio mode, keys can come from the environment as a convenience. In the
hosted deployment, always pass keys per request in the `api_keys` parameter.

## Claude.ai

Settings → Connectors → Add custom connector → paste the MCPize endpoint URL.

## Supplying your keys

Every tool accepts an `api_keys` object. Supply only the providers you want eligible:

```json
{
  "api_keys": {
    "anthropic": "sk-ant-...",
    "openai": "sk-...",
    "gemini": "AIza...",
    "groq": "gsk_...",
    "mistral": "...",
    "deepseek": "sk-...",
    "qwen": "sk-...",
    "grok": "xai-..."
  }
}
```

Providers without a key are automatically excluded from routing. Start with one
or two and add more as you get accounts.

## Getting provider keys

| Provider | Console |
|----------|---------|
| Anthropic | console.anthropic.com |
| OpenAI | platform.openai.com |
| Google Gemini | aistudio.google.com |
| Groq | console.groq.com |
| Mistral | console.mistral.ai |
| DeepSeek | platform.deepseek.com |
| Alibaba Qwen | dashscope.console.aliyun.com |
| xAI Grok | console.x.ai |
