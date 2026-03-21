from fastapi import APIRouter

from .auth import router as auth_router
from .internal import router as internal_router
from .posts import router as posts_router
from .ui import router as ui_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(posts_router)
router.include_router(internal_router)
router.include_router(ui_router)
