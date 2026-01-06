import os
import io
import pytz
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles # FIXED: Added Import
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from passlib.context import CryptContext
from jose import JWTError, jwt
from PIL import Image
from dotenv import load_dotenv

# Rate Limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import models
from database import get_db
from sqlalchemy.orm import Session

# --- CONFIGURATION ---
load_dotenv()
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "owner")
SECRET_KEY = os.getenv("SECRET_KEY", "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MAX_FILE_SIZE_MB = 5
LIBYA_TZ = pytz.timezone('Africa/Tripoli')

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel_app")

# --- COMMON OBJECTS ---
templates = Jinja2Templates(directory="templates")
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- APP & MIDDLEWARE SETUP ---
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])

app = FastAPI()

# 1. Rate Limiting Middleware
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# 2. Security Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# 3. FIXED: Static Files Mount (Restores CSS and Images)
os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Generate Hash once
OWNER_HASH = pwd_context.hash(OWNER_PASSWORD)

# --- HELPER FUNCTIONS ---
def get_current_time():
    return datetime.now(LIBYA_TZ)

def log_activity(db: Session, config_id: int, user: str, action: str, target: str, details: str):
    safe_details = (details[:495] + '..') if len(details) > 500 else details
    new_log = models.AuditLog(site_config_id=config_id, timestamp=get_current_time().replace(tzinfo=None), user=user, action=action, target=target, details=safe_details)
    db.add(new_log)

def calculate_price(db: Session, config_id: int, room_id: int, start: datetime, end: datetime, count: int):
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config_id).first()
    total = 0.0; curr = start
    while curr < end:
        season = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config_id, models.SeasonalRate.room_type_id == room_id, models.SeasonalRate.start_date <= curr.date(), models.SeasonalRate.end_date >= curr.date()).first()
        if not season: season = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config_id, models.SeasonalRate.room_type_id == None, models.SeasonalRate.start_date <= curr.date(), models.SeasonalRate.end_date >= curr.date()).first()
        price = room.price_per_night * (season.multiplier if season else 1.0)
        total += price
        curr += timedelta(days=1)
    return total * count

async def validate_and_save_image(upload_file: UploadFile, destination: str, target_type: str):
    upload_file.file.seek(0, 2); file_size = upload_file.file.tell(); upload_file.file.seek(0)
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024: raise HTTPException(status_code=400, detail=f"File too large. Max size is {MAX_FILE_SIZE_MB}MB")
    content = upload_file.file.read()
    try: img = Image.open(io.BytesIO(content)); img.verify(); img = Image.open(io.BytesIO(content))
    except Exception: raise HTTPException(status_code=400, detail="Invalid image file")
    if img.mode != 'RGB': img = img.convert('RGB')
    if target_type == 'hero': ar = img.width/img.height; w = int(450*ar); img = img.resize((w, 450), Image.Resampling.LANCZOS)
    else: img.thumbnail((250, 250))
    img.save(destination, quality=85, optimize=True)

# --- AUTHENTICATION ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta: expire = datetime.utcnow() + expires_delta
    else: expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user_token(request: Request):
    token = request.cookies.get("access_token")
    if not token: return None
    if token.startswith("Bearer "): token = token.split(" ")[1]
    return token

def verify_session(request: Request, db: Session):
    token = get_current_user_token(request)
    if not token: return None 
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        
        if username is None: return None
        if role == "admin_owner":
            return {"config": None, "user": models.User(username="SiteOwner", role="admin"), "is_owner": True}
            
        user = db.query(models.User).filter(models.User.username == username).first()
        if user is None: return None
        
        # Verify context match
        path = request.url.path
        if "/app/" in path:
            parts = path.split("/")
            if len(parts) > 2:
                ext_in_url = parts[2]
                if user.config.extension != ext_in_url: return None 
                    
        return {"config": user.config, "user": user, "is_owner": False}
    except JWTError: return None

# Dependencies
def get_config(extension: str, db: Session = Depends(get_db)):
    config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first()
    if not config: raise HTTPException(status_code=404, detail="Hotel not found")
    return config

def verify_hotel_admin(request: Request, db: Session = Depends(get_db)):
    session_data = verify_session(request, db)
    if not session_data:
        path = request.url.path
        login_url = "/owner_login"
        if "/app/" in path:
            parts = path.split("/")
            if len(parts) > 2: login_url = f"/app/{parts[2]}/login"
        raise HTTPException(status_code=303, headers={"Location": login_url})
        
    if session_data['is_owner']:
        path = request.url.path
        if "/app/" in path:
            parts = path.split("/")
            if len(parts) > 2:
                ext = parts[2]
                config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == ext).first()
                if config: session_data['config'] = config
    return session_data

def verify_owner(request: Request, db: Session = Depends(get_db)):
    session_data = verify_session(request, db)
    if not session_data or not session_data['is_owner']:
         raise HTTPException(status_code=303, headers={"Location": "/owner_login"})
    return True
