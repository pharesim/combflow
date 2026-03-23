from fastapi import APIRouter

from .posts import router as posts_router
from .ui import router as ui_router

router = APIRouter()
router.include_router(posts_router)
router.include_router(ui_router)
