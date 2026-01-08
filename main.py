import asyncio
import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from datetime import timedelta
from sqlalchemy.orm import Session

import models
from database import engine, get_db, SessionLocal
# Import utilities from core (BUT NOT APP)
from core import limiter, templates, get_config, verify_session, create_access_token, pwd_context, OWNER_HASH, ACCESS_TOKEN_EXPIRE_MINUTES, get_current_time, log_activity
from routers import public, admin, owner, api

# Initialize DB (Optional if using Alembic, can be commented out)
# models.Base.metadata.create_all(bind=engine)

# --- BACKGROUND TASK: CLEANUP EXPIRED BOOKINGS ---
async def cancel_expired_bookings_task():
    while True:
        try:
            # Run check every 60 seconds
            await asyncio.sleep(60)
            
            db = SessionLocal()
            now = get_current_time()
            
            # Find all pending bookings
            pending_bookings = db.query(models.Booking).filter(models.Booking.status == 'pending').all()
            
            for b in pending_bookings:
                # Calculate expiration time
                hours = b.config.booking_expiration_hours
                
                created_at = b.created_at
                cutoff_time = created_at + timedelta(hours=hours)
                
                is_expired = False
                # Logic to handle naive vs aware datetimes
                if created_at.tzinfo is None:
                    if cutoff_time < now.replace(tzinfo=None):
                        is_expired = True
                else:
                    if cutoff_time < now:
                        is_expired = True
                
                if is_expired:
                    b.status = 'cancelled'
                    b.notes = (b.notes or "") + "\n[System] Auto-Cancelled (Expired)"
                    log_activity(db, b.site_config_id, "System", "Auto-Cancel", b.booking_code, "Booking time expired")
            
            db.commit()
            db.close()
            
        except Exception as e:
            print(f"Background Task Error: {e}")

# --- LIFESPAN MANAGER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP LOGIC
    # 1. Ensure directories exist
    os.makedirs("static/uploads", exist_ok=True)
    os.makedirs("static/css", exist_ok=True)
    
    # 2. Launch background task
    task = asyncio.create_task(cancel_expired_bookings_task())
    yield
    # SHUTDOWN LOGIC
    task.cancel()

# --- APP INITIALIZATION ---
# This is the SINGLE point of app creation
app = FastAPI(lifespan=lifespan)

# --- MIDDLEWARE SETUP ---
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# --- STATIC FILES ---
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- INCLUDE ROUTERS ---
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(owner.router)
app.include_router(api.router)

# --- ROOT ROUTE: LANDING PAGE ---
@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

# --- LOGIN ROUTES ---
@app.get("/owner_login")
def owner_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "hotel_name": "Site Owner", "action_url": "/login_action", "context": "owner"})

@app.get("/app/{extension}/login")
def hotel_login_page(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    return templates.TemplateResponse("login.html", {"request": request, "hotel_name": config.hotel_name, "action_url": "/login_action", "context": extension})

@app.post("/login_action")
def login_action(username: str = Form(...), password: str = Form(...), context: str = Form(...), db: Session = Depends(get_db)):
    user = None; role = "staff"; config_id = None
    if context == "owner":
        if username == "owner" and pwd_context.verify(password, OWNER_HASH): role = "admin_owner"
        else: return RedirectResponse("/owner_login?error=Invalid+Credentials", status_code=303)
    else:
        config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == context).first()
        if not config: return RedirectResponse(f"/app/{context}/login?error=Hotel+Not+Found", status_code=303)
        if username == "owner" and pwd_context.verify(password, OWNER_HASH): role = "admin_owner"; config_id = config.id
        else:
            user = db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first()
            if not user or not pwd_context.verify(password, user.password_hash): return RedirectResponse(f"/app/{context}/login?error=Invalid+Credentials", status_code=303)
            role = user.role; config_id = config.id
            
    access_token = create_access_token(data={"sub": username, "role": role, "config_id": config_id}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    target = "/owner" if context == "owner" else f"/app/{context}/admin"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return response

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
