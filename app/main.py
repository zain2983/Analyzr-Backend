from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Analyzr-Backend",
    description="I HATE CSVs",
    version="0.1.0",
)

# CORS (for Next.js frontend)
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["https://analyzr-z1.vercel.app"],  # PRODUCTION                                                
    allow_origins=["https://analyzr-z1.vercel.app", "http://localhost:3000"], # DEVELOPMENT  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (to be implemented)
from app.api import sql_query, upload, check_commas_script

@app.get("/")
def root():
    return {
        # "status": "ok",
        "message": "BACKEND IS LIVE"
    }

app.include_router(upload.router, prefix="/api")
app.include_router(sql_query.router, prefix="/api")
app.include_router(check_commas_script.router, prefix="/api")
