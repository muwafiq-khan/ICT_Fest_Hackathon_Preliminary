"""CoWork API application entrypoint."""
from fastapi import FastAPI

from .database import Base, SessionLocal, engine
from .errors import AppError, app_error_handler
from .routers import admin, auth, bookings, health, rooms
from .services.reference import init_counter_from_db

Base.metadata.create_all(bind=engine)

db = SessionLocal()
try:
    init_counter_from_db(db)
finally:
    db.close()

app = FastAPI(title="CoWork API", version="1.0.0")

app.add_exception_handler(AppError, app_error_handler)


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(bookings.router)
app.include_router(admin.router)
