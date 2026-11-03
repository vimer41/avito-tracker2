from aiogram import Router
from .common import router as common_router
from .searches import router as searches_router
from .admin import router as admin_router
from .support import router as support_router
from .payments import router as payments_router
from .group_delivery import router as group_delivery_router

main_router = Router()
# First: in groups it answers /start and swallows everything else.
main_router.include_router(group_delivery_router)
main_router.include_router(payments_router)
main_router.include_router(admin_router)
main_router.include_router(support_router)
main_router.include_router(common_router)
main_router.include_router(searches_router)
