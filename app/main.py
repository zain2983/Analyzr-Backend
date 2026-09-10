import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Analyzr-Backend",
    description="I HATE CSVs",
    version="0.1.0",
)

# Hard ceiling on any request body, enforced before FastAPI buffers the
# upload. /api/upload caps itself at 20 MB while reading; this is the outer
# bound that also covers the JSON endpoints.
MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024

logger = logging.getLogger("analyzr")

PRODUCTION_ORIGINS = ["https://analyzr-z1.vercel.app"]
LOCAL_DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


def _allowed_origins() -> list[str]:
    """Origins permitted to call this API.

    The deployed service used to hardcode http://localhost:3000 alongside the
    production origin. That leaves the live API trusting any page a victim
    happens to be serving on their own machine, for a convenience only
    developers need.

    So: ALLOWED_ORIGINS (comma-separated) wins if set; otherwise ANALYZR_ENV
    decides. The default is production-only — the safe end to fail toward —
    and local development opts in explicitly with ANALYZR_ENV=development.
    """
    configured = os.getenv("ALLOWED_ORIGINS", "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]

    if os.getenv("ANALYZR_ENV", "production").strip().lower() == "development":
        return PRODUCTION_ORIGINS + LOCAL_DEV_ORIGINS

    return PRODUCTION_ORIGINS


ALLOWED_ORIGINS = _allowed_origins()

# A CORS refusal surfaces in the browser as an unexplained "Backend
# unreachable", so say out loud which origins are live. Cheap to log once,
# and it turns a confusing dead end into an obvious one.
logger.warning("CORS allowed origins: %s", ", ".join(ALLOWED_ORIGINS))


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # There are no cookies, sessions or Authorization headers in this API —
    # every request is anonymous. Leaving credentials on would let a browser
    # attach a user's ambient credentials to a cross-origin call for no gain.
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
    # Response headers are invisible to browser JS across origins unless
    # explicitly exposed — the query-export endpoint uses this one to tell
    # the frontend its download got capped, without needing a JSON body.
    expose_headers=["X-Result-Truncated"],
    max_age=600,
)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})

    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # This API only ever returns JSON and CSV attachments, so lock the browser
    # out of interpreting a response as anything renderable.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; sandbox",
    )
    return response


from app.api import sql_query, upload, check_commas_script, download, datasets, transform, query_export


@app.get("/")
def root():
    return {
        "message": "BACKEND IS LIVE"
    }


app.include_router(upload.router, prefix="/api")
app.include_router(sql_query.router, prefix="/api")
app.include_router(check_commas_script.router, prefix="/api")
app.include_router(download.router, prefix="/api")
app.include_router(datasets.router, prefix="/api")
app.include_router(transform.router, prefix="/api")
app.include_router(query_export.router, prefix="/api")
