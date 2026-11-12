"""Optional cloud launcher; the app entry point is app.py."""
import os
import uvicorn
from app import server

if __name__ == "__main__":
    uvicorn.run(
        server,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8050")),
    )
