import os
import io
import pytz
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, HTTPException, Depends, UploadFile
from fastapi.templating import Jinja2Templates

from passlib.context import CryptContext
from jose import JWTError, jwt
from PIL import Image
from dotenv import load_dotenv

# Rate Limiting Utilities
from slowapi import Limiter
from slowapi.util import get_remote_address

import models
from database import get_db
from sqlalchemy.orm import Session
from sqlalchemy import func # ADDED: Required for check_inventory_availability

# --- CONFIGURATION ---
load_dotenv()
OWNER_HASH = os.getenv("OWNER_HASH")
SECRET_KEY = os.getenv("SECRET_KEY")
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

# --- LIMITER INSTANCE ---
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])

# --- HELPER FUNCTIONS ---
def get_current_time():
    return datetime.now(LIBYA_TZ)

def log_activity(db: Session, config_id: int, user: str, action: str, target: str, details: str):
    safe_details = (details[:495] + '..') if len(details) > 500 else details
    
    ts = get_current_time().replace(tzinfo=None) 
    new_log = models.AuditLog(site_config_id=config_id, timestamp=ts, user=user, action=action, target=target, details=safe_details)
    db.add(new_log)

def calculate_price(db: Session, config_id: int, room_id: int, start: datetime, end: datetime, count: int):
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config_id).first()
    if not room: return 0.0
    
    total = 0.0; curr = start
    while curr < end:
        # Check for season overlap
        season = db.query(models.SeasonalRate).filter(
            models.SeasonalRate.site_config_id == config_id, 
            models.SeasonalRate.room_type_id == room_id, 
            models.SeasonalRate.start_date <= curr.date(), 
            models.SeasonalRate.end_date >= curr.date()
        ).first()
        
        # Fallback to global season
        if not season: 
            season = db.query(models.SeasonalRate).filter(
                models.SeasonalRate.site_config_id == config_id, 
                models.SeasonalRate.room_type_id == None, 
                models.SeasonalRate.start_date <= curr.date(), 
                models.SeasonalRate.end_date >= curr.date()
            ).first()
            
        price = room.price_per_night * (season.multiplier if season else 1.0)
        total += price
        curr += timedelta(days=1)
    return total * count

def check_inventory_availability(db: Session, config_id: int, room_type_id: int, start_date: datetime, end_date: datetime, total_qty: int):
    """
    Checks if enough rooms are available for the given date range.
    Returns (True/False, available_count).
    """
    curr = start_date
    min_availability = total_qty
    
    while curr < end_date:
        next_day = curr + timedelta(days=1)
        
        # Count confirmed/pending bookings that overlap with this specific day/slot
        occupied_count = db.query(func.count(models.Booking.id)).filter(
            models.Booking.site_config_id == config_id, 
            models.Booking.room_type_id == room_type_id, 
            models.Booking.status.in_(['confirmed', 'pending', 'checked_in']), 
            models.Booking.check_in < next_day, 
            models.Booking.check_out > curr
        ).scalar()
        
        # Count maintenance blocks
        blocked_count = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(
            models.MaintenanceBlock.room_type_id == room_type_id, 
            models.MaintenanceBlock.start_date < next_day.date(), 
            models.MaintenanceBlock.end_date > curr.date()
        ).scalar() or 0
        
        available_tonight = total_qty - occupied_count - blocked_count
        
        if available_tonight <= 0: return False, 0
        if available_tonight < min_availability: min_availability = available_tonight
        
        curr = next_day
        
    return True, min_availability

def validate_and_save_image(upload_file: UploadFile, destination: str, target_type: str):
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
    
    now = get_current_time()
    if expires_delta: 
        expire = now + expires_delta
    else: 
        expire = now + timedelta(minutes=15)
    
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user_token(request: Request):
    token = request.cookies.get("access_token")
    if not token: return None
    if token.startswith("Bearer "): token = token.split(" ")[1]
    return token

def verify_session(request: Request, db: Session):
    token = get_current_user_token(request)
    if not token: 
        return None 
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        config_id = payload.get("config_id")
        
        if username is None: 
            return None
        if role == "admin_owner":
            return {"config": None, "user": models.User(username="SiteOwner", role="admin"), "is_owner": True}
            
        user = db.query(models.User).filter(models.User.username == username,
                                            models.User.site_config_id == config_id).first()
        if user is None: return None
        
        path = request.url.path
        parts = path.split("/")

        if len(parts) > 1 and parts[1]:
            ext_in_url = parts[1]
            if user.config.extension != ext_in_url:
                return None
                    
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
        parts = path.split("/")

        if len(parts) > 1 and parts[1]:
            login_url = f"/{parts[1]}/login"
        raise HTTPException(status_code=303, headers={"Location": login_url})
        
    if session_data['is_owner']:
        path = request.url.path
        parts = path.split("/")
        if len(parts) > 1 and parts[1]:
            ext = parts[1]
            config = db.query(models.SiteConfig).filter(
                models.SiteConfig.extension == ext
            ).first()
            if config: session_data['config'] = config
    return session_data

def verify_owner(request: Request, db: Session = Depends(get_db)):
    session_data = verify_session(request, db)
    if not session_data or not session_data['is_owner']:
         raise HTTPException(status_code=303, headers={"Location": "/owner_login"})
    return True

def process_expired_bookings(db: Session, config_id: Optional[int] = None):
    try:
        now_libya = get_current_time()
        query = db.query(models.Booking).filter(models.Booking.status == 'pending')
        
        if config_id:
            query = query.filter(models.Booking.site_config_id == config_id)
            
        pending_bookings = query.all()
        
        for b in pending_bookings:
            hours = b.config.booking_expiration_hours
            created_at = b.created_at
            
            if created_at.tzinfo is None:
                created_at = LIBYA_TZ.localize(created_at)
            else:
                created_at = created_at.astimezone(LIBYA_TZ)
            
            cutoff_time = created_at + timedelta(hours=hours)
            
            if cutoff_time < now_libya:
                b.status = 'cancelled'
                b.notes = (b.notes or "") + "\n[System] Auto-Cancelled (Expired)"
                log_activity(db, b.site_config_id, "System", "Auto-Cancel", b.booking_code, "Booking time expired")
        
        db.commit()
    except Exception as e:
        print(f"Expiration Check Error: {e}")
        db.rollback()