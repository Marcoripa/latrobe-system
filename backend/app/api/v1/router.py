from fastapi import APIRouter

from app.api.v1 import inventory, projects, shifts, webhooks

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(shifts.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(inventory.router)
api_v1_router.include_router(webhooks.router)
