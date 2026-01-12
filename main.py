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
from datetime import datetime, timedelta, timezone # ADDED timezone
from sqlalchemy.orm import Session

import models
from database import engine, get_db, SessionLocal
from core import (limiter, templates, get_config, verify_session, 
                  create_access_token, pwd_context, OWNER_HASH, ACCESS_TOKEN_EXPIRE_MINUTES, 
                  get_current_time, log_activity, LIBYA_TZ, process_expired_bookings)

from routers import public, admin, owner, api

os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/css", exist_ok=True)

try:
    models.Base.metadata.create_all(bind=engine)
except: 
    pass
origins = [
    "https://www.libyanbooking.com",
    "https://libyanbooking.com",
    "http://localhost:8000", # Keep this only for local testing
]

allowed_hosts=[
        "libyanbooking.com",
        "www.libyanbooking.com",
        "localhost",
        "127.0.0.1",
    ],
# --- BACKGROUND TASK: CLEANUP EXPIRED BOOKINGS ---
async def cancel_expired_bookings_task():
    while True:
        try:
            await asyncio.sleep(300) # Sleep 5 minutes
            
            db = SessionLocal()
            # Call the centralized logic
            process_expired_bookings(db) 
            db.close()
            
        except Exception as e:
            print(f"Background Task Error: {e}")

# --- LIFESPAN MANAGER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cancel_expired_bookings_task())
    yield
    task.cancel()

# --- APP SETUP ---
app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(public.router)
app.include_router(admin.router)
app.include_router(owner.router)
app.include_router(api.router)

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

# Login Routes
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
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, secure=True, samesite="lax")
    return response

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
