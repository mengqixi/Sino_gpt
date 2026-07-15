import mimetypes

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_DIR, RESULT_DIR, UPLOAD_DIR
from .database import init_db
from .routers import api_configs, ecommerce, generate, history, prompts, recolor, upload, vip_organizer
from .seed_prompts import seed_prompt_templates


mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/jpeg", ".jpg")
mimetypes.add_type("image/jpeg", ".jpeg")
mimetypes.add_type("image/png", ".png")

app = FastAPI(title="Bag Relay Image Tool")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_prompt_templates()


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(upload.router)
app.include_router(prompts.router)
app.include_router(api_configs.router)
app.include_router(generate.router)
app.include_router(history.router)
app.include_router(recolor.router)
app.include_router(ecommerce.router)
app.include_router(vip_organizer.router)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/results", StaticFiles(directory=RESULT_DIR), name="results")

FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
