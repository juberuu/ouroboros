"""Multi-model review tool — sends code/text to multiple LLMs for consensus review.

Models are NOT hardcoded — the LLM chooses which models to use based on
prompt guidance. Budget is tracked via llm_usage events.
"""

import os
import json
import asyncio
import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_PROMPT = (
    "Review this code for bugs, security issues, architectural problems, "
    "and compliance with the project's principles. Be strict. "
    "Output PASS or FAIL with specific issues."
)


def get_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "multi_model_review",
                "description": (
                    "Send code or text to multiple LLM models for review/consensus. "
                    "Each model reviews independently. Returns all verdicts. "
                    "Choose models yourself based on the task — e.g. mix of "
                    "reasoning models (o3, gemini-2.5-pro) and code models "
                    "(claude-sonnet-4, gpt-4.1). Budget is tracked automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The code or text to review",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Custom review prompt. If omitted, uses default "
                                "review prompt checking for bugs, security, architecture."
                            ),
                        },
                        "models": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "OpenRouter model identifiers to query. "
                                "Example: ['openai/o3', 'google/gemini-2.5-pro', "
                                "'anthropic/claude-sonnet-4']"
                            ),
                        },
                    },
                    "required": ["content", "models"],
                },
            },
        }
    ]


async def handle(name, args, ctx):
    if name == "multi_model_review":
        return await _multi_model_review(args, ctx)


async def _query_model(client, model, messages, api_key):
    """Query a single model. Returns (model, response_dict) or (model, error_str)."""
    try:
        resp = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            },
            timeout=120.0,
        )
        if resp.status_code != 200:
            return model, f"HTTP {resp.status_code}: {resp.text[:500]}"
        data = resp.json()
        return model, data
    except Exception as e:
        return model, f"Error: {e}"


async def _multi_model_review(args, ctx):
    content = args.get("content", "")
    prompt = args.get("prompt", DEFAULT_PROMPT)
    models = args.get("models", [])

    if not content:
        return "Error: content is required"
    if not models:
        return "Error: models list is required (e.g. ['openai/o3', 'google/gemini-2.5-pro'])"

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return "Error: OPENROUTER_API_KEY not set"

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content},
    ]

    # Query all models concurrently
    async with httpx.AsyncClient() as client:
        tasks = [_query_model(client, m, messages, api_key) for m in models]
        results = await asyncio.gather(*tasks)

    # Process results
    output_parts = []
    for model, result in results:
        if isinstance(result, str):
            # Error case
            output_parts.append(f"### {model}\n**ERROR**: {result}")
            continue

        # Extract response text
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            text = f"(unexpected response format: {json.dumps(result)[:300]})"

        # Extract usage for budget tracking
        usage = result.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        # Estimate cost from usage (OpenRouter includes it in some responses)
        # Try to get cost from response metadata
        cost = 0.0
        if "usage" in result and "total_cost" in result["usage"]:
            cost = float(result["usage"]["total_cost"])
        elif "x-openrouter-cost" in result:
            cost = float(result["x-openrouter-cost"])

        # Emit llm_usage event for budget tracking
        if hasattr(ctx, "pending_events"):
            ctx.pending_events.append({
                "type": "llm_usage",
                "task_id": getattr(ctx, "task_id", None),
                "usage": {
                    "cost": cost,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "rounds": 1,
                    "model": model,
                },
            })

        output_parts.append(
            f"### {model}\n"
            f"*Tokens: {prompt_tokens} in / {completion_tokens} out"
            f"{f', est. ${cost:.4f}' if cost else ''}*\n\n"
            f"{text}"
        )

    separator = "\n\n---\n\n"
    header = f"## Multi-Model Review ({len(models)} models)\n\n"
    return header + separator.join(output_parts)
