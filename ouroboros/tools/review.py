"""Multi-model review tool — sends code/text to multiple LLMs for consensus review.

Models are NOT hardcoded — the LLM chooses which models to use based on
prompt guidance. Budget is tracked via llm_usage events.
"""

import os
import json
import asyncio
import httpx


# Maximum number of models allowed per review
MAX_MODELS = 10
# Concurrency limit for parallel requests
CONCURRENCY_LIMIT = 5


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def get_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "multi_model_review",
                "description": (
                    "Send code or text to multiple LLM models for review/consensus. "
                    "Each model reviews independently. Returns structured verdicts. "
                    "Choose diverse models yourself. Budget is tracked automatically."
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
                                "Review instructions — what to check for. "
                                "Fully specified by the LLM at call time."
                            ),
                        },
                        "models": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "OpenRouter model identifiers to query "
                                "(e.g. 3 diverse models for good coverage)"
                            ),
                        },
                    },
                    "required": ["content", "prompt", "models"],
                },
            },
        }
    ]


async def handle(name, args, ctx):
    if name == "multi_model_review":
        return await _multi_model_review(args, ctx)
    else:
        return {"error": f"Unknown tool: {name}"}


async def _query_model(client, model, messages, api_key, semaphore):
    """Query a single model with semaphore-based concurrency control. Returns (model, response_dict, headers_dict) or (model, error_str, None)."""
    async with semaphore:
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

            # Extract ALL data while client is still open
            status_code = resp.status_code
            response_text = resp.text
            response_headers = dict(resp.headers)

            if status_code != 200:
                error_text = response_text[:200]
                if len(response_text) > 200:
                    error_text += " [truncated]"
                return model, f"HTTP {status_code}: {error_text}", None

            data = resp.json()
            return model, data, response_headers
        except asyncio.TimeoutError:
            return model, "Error: Timeout after 120s", None
        except Exception as e:
            error_msg = str(e)[:200]
            if len(str(e)) > 200:
                error_msg += " [truncated]"
            return model, f"Error: {error_msg}", None


async def _multi_model_review(args, ctx):
    content = args.get("content", "")
    prompt = args.get("prompt", "")
    models = args.get("models", [])

    if not content:
        return {"error": "content is required"}
    if not prompt:
        return {"error": "prompt is required"}
    if not models:
        return {"error": "models list is required (e.g. ['openai/o3', 'google/gemini-2.5-pro'])"}

    # Validate models list elements
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return {"error": "models must be a list of strings"}

    # Check model count limit
    if len(models) > MAX_MODELS:
        return {"error": f"Too many models requested ({len(models)}). Maximum is {MAX_MODELS}."}

    if len(models) == 0:
        return {"error": "At least one model is required"}

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"error": "OPENROUTER_API_KEY not set"}

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content},
    ]

    # Query all models with bounded concurrency
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    async with httpx.AsyncClient() as client:
        tasks = [_query_model(client, m, messages, api_key, semaphore) for m in models]
        results = await asyncio.gather(*tasks)

    # Process results into structured data
    review_results = []
    for model, result, headers_dict in results:
        if isinstance(result, str):
            # Error case
            review_result = {
                "model": model,
                "verdict": "ERROR",
                "text": result,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_estimate": 0.0,
            }
        else:
            # Extract response text
            try:
                choices = result.get("choices", [])
                if not choices:
                    text = f"(no choices in response: {json.dumps(result)[:200]})"
                    verdict = "ERROR"
                else:
                    text = choices[0]["message"]["content"]
                    # Robust verdict parsing: check first 3 lines for PASS/FAIL anywhere (case-insensitive)
                    verdict = "UNKNOWN"
                    lines = text.split("\n")[:3]  # Check only first 3 lines
                    for line in lines:
                        line_upper = line.upper()
                        if "PASS" in line_upper:
                            verdict = "PASS"
                            break
                        elif "FAIL" in line_upper:
                            verdict = "FAIL"
                            break
            except (KeyError, IndexError, TypeError):
                error_text = json.dumps(result)[:200]
                if len(json.dumps(result)) > 200:
                    error_text += " [truncated]"
                text = f"(unexpected response format: {error_text})"
                verdict = "ERROR"

            # Extract usage for budget tracking
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # Extract cost from response body (preferred) or headers
            cost = 0.0
            try:
                # First check response body for usage.cost
                if "usage" in result and "cost" in result["usage"]:
                    cost = float(result["usage"]["cost"])
                # Fallback to total_cost field
                elif "usage" in result and "total_cost" in result["usage"]:
                    cost = float(result["usage"]["total_cost"])
                # Finally check headers
                elif headers_dict:
                    # Case-insensitive search for cost header
                    for key, value in headers_dict.items():
                        if key.lower() == "x-openrouter-cost":
                            cost = float(value)
                            break
            except (ValueError, TypeError, KeyError):
                pass

            review_result = {
                "model": model,
                "verdict": verdict,
                "text": text,
                "tokens_in": prompt_tokens,
                "tokens_out": completion_tokens,
                "cost_estimate": cost,
            }

        # Emit llm_usage event for budget tracking (for ALL cases, including errors)
        if hasattr(ctx, "pending_events"):
            ctx.pending_events.append({
                "type": "llm_usage",
                "task_id": getattr(ctx, "task_id", None),
                "usage": {
                    "cost": review_result["cost_estimate"],
                    "prompt_tokens": review_result["tokens_in"],
                    "completion_tokens": review_result["tokens_out"],
                    "rounds": 1,
                    "model": model,
                },
            })

        review_results.append(review_result)

    return {
        "model_count": len(models),
        "results": review_results,
    }
