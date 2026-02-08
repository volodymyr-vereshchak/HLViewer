"""Clean start of FastAPI application"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,  # No reload to avoid caching issues
        log_level="info"
    )
