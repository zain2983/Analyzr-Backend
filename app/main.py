from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Analyze-Backend",
    description="FUCK CSVs",
    version="0.1.0",
)

# CORS (for Next.js frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (to be implemented)
from app.api import upload, query

app.include_router(upload.router, prefix="/api")
app.include_router(query.router, prefix="/api")
