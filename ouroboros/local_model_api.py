"""Local model management API endpoints, extracted from server.py."""

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse


async def api_local_model_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        source = body.get("source", "").strip()
        filename = body.get("filename", "").strip()
        port = int(body.get("port", 8766))
        n_gpu_layers = int(body.get("n_gpu_layers", -1))
        n_ctx = int(body.get("n_ctx", 0))
        chat_format = body.get("chat_format", "").strip()

        if not source:
            return JSONResponse({"error": "source is required"}, status_code=400)

        from ouroboros.local_model import get_manager, _get_runtime_hint
        mgr = get_manager()

        if mgr.is_running:
            return JSONResponse({"error": "Local model server is already running"}, status_code=409)

        # Preflight: check llama-cpp-python is installed BEFORE downloading the model.
        # This prevents users from waiting through a large download only to hit an
        # install error at the end.
        # Run in a thread to avoid blocking the async event loop (subprocess.run, 15s timeout).
        runtime_ok = await asyncio.to_thread(mgr.check_runtime)
        if not runtime_ok:
            return JSONResponse(
                {
                    "error": "runtime_missing",
                    "message": (
                        "llama-cpp-python is not installed. "
                        "Use the 'Install Local Runtime' button to install it first."
                    ),
                    "hint": _get_runtime_hint(),
                },
                status_code=412,
            )

        # Download can be slow, run in thread to not block the async event loop
        model_path = await asyncio.to_thread(mgr.download_model, source, filename)

        mgr.start_server(model_path, port=port, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx, chat_format=chat_format)
        return JSONResponse({"status": "starting", "model_path": model_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_stop(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        get_manager().stop_server()
        return JSONResponse({"status": "stopped"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_status(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        mgr = get_manager()
        # If runtime status is still unknown and no operation is running,
        # run a quick probe so the Settings page can surface the Install button
        # on the very first poll — before the user clicks Start.
        if mgr._runtime_status == "unknown" and mgr.get_status() == "offline":
            await asyncio.to_thread(mgr.check_runtime)
        return JSONResponse(mgr.status_dict())
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


async def api_local_model_test(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        mgr = get_manager()
        if not mgr.is_running:
            return JSONResponse({"error": "Local model server is not running"}, status_code=400)
        result = mgr.test_tool_calling()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_install_runtime(request: Request) -> JSONResponse:
    """Start an async install of llama-cpp-python into the app-managed interpreter.

    The install runs in a background thread tracked on the manager.  Callers
    should poll ``/api/local-model/status`` and watch ``runtime_status``:

    - ``"installing"``   — install in progress
    - ``"install_ok"``   — install succeeded; caller may now start the model
    - ``"install_error"``— install failed; ``runtime_install_log`` has details
    """
    try:
        from ouroboros.local_model import get_manager
        mgr = get_manager()

        current = mgr._runtime_status
        if current == "installing":
            return JSONResponse({"status": "already_installing"})

        mgr.install_runtime()
        return JSONResponse({"status": "installing"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
