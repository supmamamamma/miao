import os
import re
import httpx
from fastapi import FastAPI, Request, HTTPException, Security, Header
from fastapi.responses import StreamingResponse, Response, FileResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
import logging
from fastapi.staticfiles import StaticFiles
from itertools import cycle
import asyncio
import json
from pathlib import Path

# --- Configuration ---
PROXY_API_KEY = os.environ.get("PROXY_API_KEY")
VERTEX_EXPRESS_KEYS_STR = os.environ.get("VERTEX_EXPRESS_KEYS")
VERTEX_EXPRESS_KEYS = [key.strip() for key in VERTEX_EXPRESS_KEYS_STR.split(',')] if VERTEX_EXPRESS_KEYS_STR else []

if not VERTEX_EXPRESS_KEYS:
    raise ValueError("VERTEX_EXPRESS_KEYS environment variable not set or empty.")

# --- Globals ---
app = FastAPI()
project_id_cache = {}
key_rotator = cycle(VERTEX_EXPRESS_KEYS)
key_lock = asyncio.Lock()
logger = logging.getLogger(__name__)

# --- API Key Security ---
api_key_query = APIKeyQuery(name="key", auto_error=False)
api_key_header = APIKeyHeader(name="x-goog-api-key", auto_error=False)

async def get_api_key(
    key_query: str = Security(api_key_query),
    key_header: str = Security(api_key_header),
):
    if PROXY_API_KEY:
        if key_query == PROXY_API_KEY:
            return key_query
        if key_header == PROXY_API_KEY:
            return key_header
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    else:
        # If no PROXY_API_KEY is set, authentication is skipped
        return None

# --- Project ID Extraction ---
async def get_project_id(key: str):
    if key in project_id_cache:
        return project_id_cache[key]

    url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-2.6-pro:generateContent?key={key}"
    headers = {'Content-Type': 'application/json'}
    data = '{}'

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, data=data)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                error_message = e.response.json().get("error", {}).get("message", "")
                match = re.search(r"projects/([^/]+)/locations/", error_message)
                if match:
                    project_id = match.group(1)
                    project_id_cache[key] = project_id
                    return project_id
            raise HTTPException(status_code=500, detail=f"Failed to extract project ID: {e.response.text}")
    
    raise HTTPException(status_code=500, detail="Could not extract project ID from any key.")

# --- Frontend Routes ---
@app.get("/")
async def frontend():
    # Get the directory where this script is located
    current_dir = Path(__file__).parent
    html_path = current_dir / "index.html"
    return FileResponse(html_path)

@app.get("/gif.worker.js")
async def gif_worker():
    # Serve the GIF worker script
    current_dir = Path(__file__).parent
    worker_path = current_dir / "gif.worker.js"
    return FileResponse(worker_path, media_type="application/javascript")

# --- Shared Model Calling Logic ---
async def call_model(request: Request, model_path: str, express_key: str, project_id: str):
    """
    Shared function to handle model calling logic for both proxy endpoints.
    """
    raw_request_body = await request.body()
    request_body_to_send = raw_request_body

    try:
        request_json = json.loads(raw_request_body)
        if "gemini-2.0-flash-exp-image-generation" in model_path:
            model_path = model_path.replace("gemini-2.0-flash-exp-image-generation", "gemini-2.5-flash-image-preview")

        if "generationConfig" not in request_json:
            request_json["generationConfig"] = {}

        # Model-specific request body modification
        if "gemini-2.5-flash-image-preview" in model_path:
            if "generationConfig" in request_json and "thinkingConfig" in request_json.get("generationConfig", {}):
                del request_json["generationConfig"]["thinkingConfig"]
            if "generationConfig" in request_json and "responseMimeType" in request_json.get("generationConfig", {}):
                del request_json["generationConfig"]["responseMimeType"]
            request_json["generationConfig"]["responseModalities"] = ["TEXT", "IMAGE"]

        # Ensure contents have role field
        if "contents" in request_json:
            for content in request_json["contents"]:
                if "role" not in content:
                    content["role"] = "user"

        request_json["safetySettings"] = [
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_IMAGE_HATE",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_IMAGE_HARASSMENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE"
            }
        ]
        request_body_to_send = json.dumps(request_json).encode('utf-8')
    except json.JSONDecodeError:
        pass  # Not a json body, proxy as is

    target_url = f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/publishers/google/models/{model_path}?key={express_key}"

    client = httpx.AsyncClient(timeout=None)

    headers_to_proxy = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ['host', 'authorization', 'x-goog-api-key', 'x-vertex-express-key', 'content-length']
    }

    print(request_body_to_send)

    if "streamGenerateContent" in model_path:
        target_url = target_url + "&alt=sse"

    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=headers_to_proxy,
        content=request_body_to_send,
    )
    response = await client.send(req, stream=True)

    if response.status_code != 200:
        try:
            response_data = await response.aread()
            return Response(
                content=response_data,
                status_code=response.status_code,
                headers=dict(response.headers),
            )
        finally:
            await response.aclose()
            await client.aclose()

    if "streamGenerateContent" in model_path:
        async def stream_generator():
            try:
                async for line in response.aiter_lines():
                    print(line)
                    yield f"{line}\n"
            finally:
                await response.aclose()
                await client.aclose()
        
        return StreamingResponse(stream_generator(), media_type=response.headers.get("content-type"))
    else:
        try:
            response_data = await response.aread()
            response_json = json.loads(response_data)

            if 'candidates' in response_json:
                for candidate in response_json.get('candidates', []):
                    if 'content' in candidate and 'parts' in candidate.get('content', {}):
                        candidate['content']['parts'] = [part for part in candidate['content']['parts'] if part]

            modified_response_data = json.dumps(response_json).encode('utf-8')

            return Response(
                content=modified_response_data,
                status_code=response.status_code,
                headers={"content-type": response.headers.get("content-type")},
            )
        finally:
            await response.aclose()
            await client.aclose()

# --- Frontend-specific endpoint (no authentication required) ---
@app.post("/frontend/v1beta/models/{model_name}:{function_name}")
async def frontend_proxy(
    model_name: str,
    function_name: str,
    request: Request,
    vertex_express_key: str = Header(..., alias="x-vertex-express-key")
):
    """
    Frontend-specific proxy endpoint that only requires a Vertex Express key.
    No proxy authentication needed.
    """
    try:
        # Get or extract project ID for this key
        project_id = await get_project_id(vertex_express_key)
        
        # Use shared model calling logic
        model_path = f"{model_name}:{function_name}"
        return await call_model(request, model_path, vertex_express_key, project_id)
            
    except Exception as e:
        logger.error(f"Frontend proxy error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

# --- Proxy Endpoint ---
@app.post("/v1beta/models/{model_path:path}")
async def proxy(request: Request, model_path: str, _: str = Security(get_api_key)):
    async with key_lock:
        express_key = next(key_rotator)
    
    project_id = await get_project_id(express_key)
    
    # Use shared model calling logic
    return await call_model(request, model_path, express_key, project_id)

if __name__ == "__main__":
    import uvicorn
    # Hugging Face Spaces run on port 7860
    uvicorn.run(app, host="0.0.0.0", port=7860)