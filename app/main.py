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
    allow_origins=["https://analyzr-z1.vercel.app"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (to be implemented)
from app.api import sql_query, upload

@app.get("/")
def root():
    return {
        # "status": "ok",
        "message": "Le - Moot diye tere frontend pe"
    }

app.include_router(upload.router, prefix="/api")
app.include_router(sql_query.router, prefix="/api")
