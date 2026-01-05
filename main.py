import shutil
import os
import io
import pytz
import uuid
import logging
from typing import List, Optional
from datetime import datetime, timedelta, date
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from PIL import Image
from passlib.context import CryptContext
from jose import JWTError, jwt
from dotenv import load_dotenv

# Rate Limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from database import engine, get_db
import models

# --- CONFIGURATION ---
load_dotenv()
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "owner")
SECRET_KEY = os.getenv("SECRET_KEY", "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MAX_FILE_SIZE_MB = 5

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel_app")

models.Base.metadata.create_all(bind=engine)

limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

LIBYA_TZ = pytz.timezone('Africa/Tripoli')
OWNER_HASH = pwd_context.hash(OWNER_PASSWORD)

os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- HELPER FUNCTIONS ---
def get_current_time():
    return datetime.now(LIBYA_TZ)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta: expire = datetime.utcnow() + expires_delta
    else: expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user_token(request: Request):
    token = request.cookies.get("access_token")
    if not token: return None
    if token.startswith("Bearer "): token = token.split(" ")[1]
    return token

def verify_session(request: Request, db: Session = Depends(get_db)):
    token = get_current_user_token(request)
    if not token: return None 
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        config_id: int = payload.get("config_id")
        
        if username is None: return None
        
        if role == "admin_owner":
            return {"config": None, "user": models.User(username="SiteOwner", role="admin"), "is_owner": True}
            
        user = db.query(models.User).filter(models.User.username == username).first()
        if user is None: return None
        
        # Verify context
        path = request.url.path
        if "/app/" in path:
            parts = path.split("/")
            if len(parts) > 2:
                ext_in_url = parts[2]
                if user.config.extension != ext_in_url:
                    return None 
                    
        return {"config": user.config, "user": user, "is_owner": False}
        
    except JWTError:
        return None

# --- DEPENDENCIES ---
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
            if len(parts) > 2:
                login_url = f"/app/{parts[2]}/login"
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

# --- LOGGING & MATH HELPERS ---
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

# --- LOGIN ROUTES ---

@app.get("/owner_login", response_class=HTMLResponse)
def owner_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "hotel_name": "Site Owner", "action_url": "/login_action", "context": "owner"})

@app.get("/app/{extension}/login", response_class=HTMLResponse)
def hotel_login_page(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    return templates.TemplateResponse("login.html", {"request": request, "hotel_name": config.hotel_name, "action_url": "/login_action", "context": extension})

@app.post("/login_action")
def login_action(username: str = Form(...), password: str = Form(...), context: str = Form(...), db: Session = Depends(get_db)):
    user = None
    role = "staff"
    config_id = None
    
    if context == "owner":
        if username == "owner" and pwd_context.verify(password, OWNER_HASH):
            role = "admin_owner"
        else: return RedirectResponse("/owner_login?error=Invalid+Credentials", status_code=303)
    else:
        config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == context).first()
        if not config: return RedirectResponse(f"/app/{context}/login?error=Hotel+Not+Found", status_code=303)
        
        if username == "owner" and pwd_context.verify(password, OWNER_HASH):
             role = "admin_owner"; config_id = config.id
        else:
            user = db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first()
            if not user or not pwd_context.verify(password, user.password_hash):
                return RedirectResponse(f"/app/{context}/login?error=Invalid+Credentials", status_code=303)
            role = user.role; config_id = config.id

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": username, "role": role, "config_id": config_id}, expires_delta=access_token_expires)
    
    target = "/owner" if context == "owner" else f"/app/{context}/admin"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return response

@app.get("/logout")
def logout(request: Request):
    referer = request.headers.get("referer")
    redirect_url = "/"
    if referer and "/app/" in referer:
        try: redirect_url = "/app/" + referer.split("/app/")[1].split("/")[0]
        except: pass
    elif referer and "owner" in referer: redirect_url = "/owner_login"
        
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.delete_cookie("access_token")
    return response

# --- EXISTING ROUTES ---

@app.get("/reset_db")
def reset_db(db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    db.query(models.AuditLog).delete(); db.query(models.Booking).delete(); db.query(models.RoomImage).delete()
    db.query(models.MaintenanceBlock).delete(); db.query(models.SeasonalRate).delete()
    db.query(models.RoomUnit).delete(); db.query(models.RoomType).delete(); db.query(models.HeroImage).delete(); db.query(models.SiteConfig).delete(); db.query(models.User).delete()
    default_hash = pwd_context.hash("password123")
    config = models.SiteConfig(admin_password_hash=default_hash, booking_expiration_hours=24, highlights="Experience paradise.", about_description="Welcome.", amenities_list="Free Wi-Fi")
    db.add(config); db.commit()
    return "Database cleared & Updated!"

@app.get("/owner", response_class=HTMLResponse)
def owner_dashboard(request: Request, db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    configs = db.query(models.SiteConfig).all(); msg = request.query_params.get("success")
    return templates.TemplateResponse("owner.html", {"request": request, "configs": configs, "msg": msg})

@app.post("/owner/create_hotel")
def create_hotel(extension: str = Form(...), name: str = Form(...), admin_pass: str = Form(...), user_pass: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    if db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first(): return "Extension exists"
    new_conf = models.SiteConfig(extension=extension, hotel_name=name)
    db.add(new_conf); db.commit()
    db.add_all([models.User(site_config_id=new_conf.id, username=f"{extension}_ad", password_hash=pwd_context.hash(admin_pass), role="admin"), models.User(site_config_id=new_conf.id, username=f"{extension}_user", password_hash=pwd_context.hash(user_pass), role="staff")]); db.commit()
    return RedirectResponse(url="/owner?success=Hotel+Created", status_code=303)

@app.post("/owner/reset_password")
def reset_hotel_password(config_id: int = Form(...), role: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    user = db.query(models.User).filter(models.User.site_config_id == config_id, models.User.role == role).first()
    if user: user.password_hash = pwd_context.hash("ResetToday"); log_activity(db, config_id, "Owner", "Password Reset", f"{role} User", "Reset"); db.commit(); return RedirectResponse(url="/owner?success=Password+Reset", status_code=303)
    return RedirectResponse(url="/owner?error=User+Not+Found", status_code=303)

@app.get("/app/{extension}", response_class=HTMLResponse)
@limiter.limit("60/minute")
def hotel_home(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    if not config.is_active: return templates.TemplateResponse("maintenance.html", {"request": request})
    return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images})

@app.post("/app/{extension}/search", response_class=HTMLResponse)
@limiter.limit("20/minute")
def hotel_search(request: Request, extension: str, check_in: str = Form(...), check_out: str = Form(...), guests: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    c_in = datetime.strptime(check_in, "%Y-%m-%d"); c_out = datetime.strptime(check_out, "%Y-%m-%d")
    if c_out <= c_in: return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Check-out must be after check-in."})
    available_rooms = []
    for r in config.rooms:
        if r.capacity < guests: continue
        booked = db.query(func.count(models.Booking.id)).filter(models.Booking.room_type_id == r.id, models.Booking.status.in_(['confirmed', 'pending']), models.Booking.check_in < c_out, models.Booking.check_out > c_in).scalar()
        blocked = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(models.MaintenanceBlock.room_type_id == r.id, models.MaintenanceBlock.start_date < c_out.date(), models.MaintenanceBlock.end_date > c_in.date()).scalar() or 0
        if (r.total_quantity - booked - blocked) > 0:
            r.dynamic_total = calculate_price(db, config.id, r.id, c_in, c_out, 1)
            r.available_now = r.total_quantity - booked - blocked
            available_rooms.append(r)
    return templates.TemplateResponse("search_results.html", {"request": request, "config": config, "rooms": available_rooms, "check_in": check_in, "check_out": check_out, "guests": guests})

@app.get("/app/{extension}/book/{room_id}", response_class=HTMLResponse)
def book_page(request: Request, extension: str, room_id: int, check_in: Optional[str] = None, check_out: Optional[str] = None, guests: int = 1, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    if not check_in or not check_out: check_in = datetime.now().strftime("%Y-%m-%d"); check_out = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room, "prefill_check_in": check_in, "prefill_check_out": check_out, "prefill_guests": guests})

@app.get("/api/calendar_events")
def get_calendar_events(start: str, end: str, room_id: Optional[int] = None, db: Session = Depends(get_db)):
    start_dt = datetime.strptime(start[:10], "%Y-%m-%d"); end_dt = datetime.strptime(end[:10], "%Y-%m-%d")
    events = []; curr = start_dt
    
    while curr < end_dt:
        nxt = curr + timedelta(days=1); check_time = curr + timedelta(hours=23, minutes=59)
        
        if room_id:
            room = db.query(models.RoomType).filter(models.RoomType.id == room_id).first()
            if not room: return JSONResponse([])
            booked = db.query(func.count(models.Booking.id)).filter(models.Booking.room_type_id == room_id, models.Booking.status.in_(['confirmed', 'pending']), models.Booking.check_in <= check_time, models.Booking.check_out > check_time).scalar()
            blocked = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(models.MaintenanceBlock.room_type_id == room_id, models.MaintenanceBlock.start_date <= curr.date(), models.MaintenanceBlock.end_date > curr.date()).scalar() or 0
            remaining = room.total_quantity - booked - blocked
        else:
            total_capacity = db.query(func.sum(models.RoomType.total_quantity)).scalar() or 0
            total_booked = db.query(func.count(models.Booking.id)).filter(models.Booking.status.in_(['confirmed', 'pending']), models.Booking.check_in <= check_time, models.Booking.check_out > check_time).scalar() or 0
            total_blocked = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(models.MaintenanceBlock.start_date <= curr.date(), models.MaintenanceBlock.end_date > curr.date()).scalar() or 0
            remaining = total_capacity - total_booked - total_blocked

        if remaining > 0: events.append({"title": "Available", "start": curr.strftime("%Y-%m-%d"), "allDay": True, "backgroundColor": "#28a745", "display": "background"})
        else: events.append({"title": "Full", "start": curr.strftime("%Y-%m-%d"), "allDay": True, "backgroundColor": "#dc3545", "display": "background"})
        curr = nxt
    return JSONResponse(events)

@app.post("/app/{extension}/api/calculate_price")
def api_calculate_price(extension: str, room_id: int = Form(...), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    try:
        c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0); c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
        if c_out <= c_in: return JSONResponse({"error": "Invalid dates"}, status_code=400)
        total = calculate_price(db, config.id, room_id, c_in, c_out, rooms_needed)
        nights = (c_out - c_in).days
        return JSONResponse({"total": total, "nights": nights})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/app/{extension}/book/confirm")
@limiter.limit("5/minute")
def book_confirm(request: Request, extension: str, room_id: int = Form(...), guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), guests_count: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    c_in = datetime.strptime(check_in, "%Y-%m-%d"); c_out = datetime.strptime(check_out, "%Y-%m-%d")
    if c_out <= c_in: return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": db.query(models.RoomType).get(room_id), "error": "Invalid dates.", "prefill_check_in": check_in, "prefill_check_out": check_out})
    all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).order_by(models.RoomUnit.label).all()
    available_units = []
    for u in all_units:
        if len(available_units) >= rooms_needed: break
        conflict = db.query(models.Booking).filter(models.Booking.room_unit_id == u.id, models.Booking.check_in < c_out, models.Booking.check_out > c_in, models.Booking.status.in_(['confirmed','pending','checked_in'])).first()
        maint = db.query(models.MaintenanceBlock).filter(models.MaintenanceBlock.room_unit_id == u.id, models.MaintenanceBlock.start_date < c_out.date(), models.MaintenanceBlock.end_date > c_in.date()).first()
        if not conflict and not maint: available_units.append(u)
    if len(available_units) < rooms_needed: return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": db.query(models.RoomType).get(room_id), "error": "Not enough rooms available.", "prefill_check_in": check_in, "prefill_check_out": check_out})
    total_one = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    total_all = total_one * rooms_needed
    nights = (c_out - c_in).days
    created_bookings = []
    for i in range(rooms_needed):
        b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
        bk = models.Booking(site_config_id=config.id, room_type_id=room_id, room_unit_id=available_units[i].id, booking_code=b_code, guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone, check_in=c_in, check_out=c_out, total_price=total_one, rooms_booked=1, guests_count=guests_count, created_at=datetime.now())
        db.add(bk); created_bookings.append(bk)
        log_activity(db, config.id, "Guest", "New Booking", b_code, f"{guest_name} booked {available_units[i].label}")
    db.commit()
    return templates.TemplateResponse("success.html", {"request": request, "config": config, "bookings": created_bookings, "total_cost": total_all, "nights": nights})

# --- ADMIN ROUTES ---
@app.get("/app/{extension}/admin", response_class=HTMLResponse)
def hotel_admin(request: Request, extension: str, sort_by: str = "check_in", search: Optional[str] = None, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']; user = context['user']
    hotel_users = db.query(models.User).filter(models.User.site_config_id == config.id).all()
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    all_units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.name, models.RoomUnit.label).all()
    seasons = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id).all()
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    today = get_current_time().date(); tomorrow = today + timedelta(days=1)
    base_q = db.query(models.Booking).filter(models.Booking.site_config_id == config.id)
    search_results = []
    if search: search_results = base_q.filter(or_(models.Booking.booking_code.ilike(f"%{search}%"), models.Booking.guest_name.ilike(f"%{search}%"))).all()
    checkins_today = base_q.filter(func.date(models.Booking.check_in) == today, models.Booking.status != 'cancelled').all()
    checkouts_today = base_q.filter(func.date(models.Booking.check_out) == today, models.Booking.status != 'cancelled').all()
    checkins_tmrw = base_q.filter(func.date(models.Booking.check_in) == tomorrow, models.Booking.status != 'cancelled').all()
    checkouts_tmrw = base_q.filter(func.date(models.Booking.check_out) == tomorrow, models.Booking.status != 'cancelled').all()
    upcoming_q = base_q.filter(func.date(models.Booking.check_in) >= today)
    upcoming = upcoming_q.order_by(models.Booking.created_at.desc()).all() if sort_by == 'created' else upcoming_q.order_by(models.Booking.check_in.asc()).all()
    active_bookings = base_q.filter(models.Booking.status == 'checked_in').order_by(models.Booking.check_out.asc()).all()
    revenue = sum([b.total_price for b in upcoming if b.status in ['confirmed', 'checked_in', 'checked_out']])
    future_dep = db.query(func.sum(models.Booking.deposit_amount)).filter(models.Booking.site_config_id == config.id, func.date(models.Booking.check_in) >= today, models.Booking.status.in_(['confirmed', 'checked_in'])).scalar() or 0.0
    confirmed_b = base_q.filter(func.date(models.Booking.check_in) >= today, models.Booking.status == 'confirmed').all()
    outstanding = sum([b.total_price - b.deposit_amount for b in confirmed_b])
    chart_labels = []; chart_data = []
    for i in range(14):
        day_t = today + timedelta(days=i); chart_labels.append(day_t.strftime("%b %d"))
        day_b = base_q.filter(func.date(models.Booking.check_in) == day_t, models.Booking.status == 'confirmed').all()
        chart_data.append(sum([b.total_price - b.deposit_amount for b in day_b]))
    logs = db.query(models.AuditLog).filter(models.AuditLog.site_config_id == config.id).order_by(models.AuditLog.timestamp.desc()).limit(100).all()
    total_capacity = sum([r.total_quantity for r in rooms])
    occupied = base_q.filter(models.Booking.check_in <= today, models.Booking.check_out > today, models.Booking.status.in_(['checked_in', 'confirmed'])).count()
    occupancy_rate = int((occupied / total_capacity * 100) if total_capacity > 0 else 0)
    recent = base_q.order_by(models.Booking.created_at.desc()).limit(5).all()
    msg = request.query_params.get("success"); err = request.query_params.get("error")
    return templates.TemplateResponse("admin.html", {"request": request, "config": config, "user": user, "hotel_users": hotel_users, "rooms": rooms, "all_units": all_units, "seasons": seasons, "blocks": blocks, "hero_images": config.images, "checkins_today_list": checkins_today, "checkouts_today_list": checkouts_today, "checkins_tomorrow_list": checkins_tmrw, "checkouts_tomorrow_list": checkouts_tmrw, "upcoming_bookings": upcoming, "active_bookings": active_bookings, "financials": {"future_deposits": round(future_dep, 2), "outstanding_balance": round(outstanding, 2), "chart_labels": chart_labels, "chart_data": chart_data, "recent_bookings": recent}, "stats": {"revenue": round(revenue,2), "occupancy": occupancy_rate}, "logs": logs, "search_results": search_results, "search_query": search, "msg": msg, "err": err, "sort_by": sort_by})

@app.get("/app/{extension}/admin/api/tape_chart")
def get_tape_chart(extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.name, models.RoomUnit.label).all()
    groups = [{"id": u.id, "content": f"<strong>{u.room_type.name}</strong> - {u.label}"} for u in units]
    bookings = db.query(models.Booking).filter(models.Booking.site_config_id == config.id, models.Booking.status.in_(['confirmed', 'pending', 'checked_in', 'checked_out'])).all()
    items = []
    for b in bookings:
        if b.room_unit_id:
            color = '#3788d8'
            if b.status == 'pending': color = '#ffc107'
            elif b.status == 'checked_in': color = '#198754'
            elif b.status == 'checked_out': color = '#6c757d'
            style = f"background-color: {color}; color: white; cursor: pointer; opacity: 0.7; border-radius: 6px; border: 1px solid rgba(0,0,0,0.1);"
            items.append({"id": b.id, "group": b.room_unit_id, "content": f"{b.guest_name}", "start": b.check_in.isoformat(), "end": b.check_out.isoformat(), "style": style})
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    for m in blocks:
        if m.room_unit_id: items.append({"id": f"maint_{m.id}", "group": m.room_unit_id, "content": "BLOCKED", "start": m.start_date.isoformat(), "end": m.end_date.isoformat(), "style": "background-color: black; opacity: 0.5; border-radius: 6px;", "type": "background"})
    return JSONResponse(content={"groups": groups, "items": items})

# --- ADMIN CRUD ---
@app.post("/app/{extension}/admin/update_site")
def update_site(extension: str, hotel_name: str = Form(...), highlights: str = Form(""), about_description: str = Form(""), amenities_list: str = Form(""), email: str = Form(""), phone: str = Form(""), address: str = Form(""), map_url: str = Form(""), facebook: str = Form(""), instagram: str = Form(""), youtube: str = Form(""), rules: str = Form(""), booking_success_message: str = Form(...), theme_id: int = Form(1), booking_expiration_hours: int = Form(24), db: Session = Depends(get_db), context: dict = Depends(verify_hotel_admin)):
    if context['user'].role != 'admin': return "Unauthorized"
    config = context['config']
    config.hotel_name = hotel_name; config.highlights = highlights; config.about_description = about_description; config.amenities_list = amenities_list
    config.contact_email = email; config.contact_phone = phone; config.address = address; config.map_url = map_url
    config.facebook = facebook; config.instagram = instagram; config.youtube = youtube; config.rules = rules; config.booking_success_message = booking_success_message
    config.theme_id = theme_id; config.booking_expiration_hours = booking_expiration_hours
    log_activity(db, config.id, "Admin", "Update Settings", "Site Config", "Settings updated")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Settings+Updated#site", status_code=303)

@app.post("/app/{extension}/admin/add_room")
async def add_room(extension: str, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(2), custom_labels: str = Form(""), images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    new_room = models.RoomType(site_config_id=config.id, name=name, price_per_night=price, total_quantity=qty, description=desc, capacity=capacity)
    db.add(new_room); db.commit()
    raw_labels = [l.strip() for l in custom_labels.split(',') if l.strip()]
    for i in range(qty):
        lbl = raw_labels[i] if i < len(raw_labels) else f"{name} #{i+1}"
        db.add(models.RoomUnit(room_type_id=new_room.id, label=lbl))
    if images:
        for img in images:
            if img.filename:
                path = f"static/uploads/room_{new_room.id}_{uuid.uuid4().hex[:6]}.jpg"
                await validate_and_save_image(img, path, "room")
                db.add(models.RoomImage(room_id=new_room.id, image_url=f"/{path}"))
    log_activity(db, config.id, "Admin", "Create Room", name, f"Created with {qty} units")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#rooms", status_code=303)

@app.post("/app/{extension}/admin/add_season")
def add_season(extension: str, name: str = Form(...), start: str = Form(...), end: str = Form(...), multiplier: float = Form(...), room_type_id: Optional[int] = Form(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    s_date = datetime.strptime(start, "%Y-%m-%d").date(); e_date = datetime.strptime(end, "%Y-%m-%d").date()
    if e_date <= s_date: return RedirectResponse(f"/app/{extension}/admin?error=End+Date+Must+Be+After+Start#seasons", status_code=303)
    conflict = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id, models.SeasonalRate.room_type_id == room_type_id, models.SeasonalRate.start_date <= e_date, models.SeasonalRate.end_date >= s_date).first()
    if conflict: return RedirectResponse(f"/app/{extension}/admin?error=Date+Overlap+With+{conflict.name}#seasons", status_code=303)
    db.add(models.SeasonalRate(site_config_id=config.id, room_type_id=room_type_id, name=name, start_date=s_date, end_date=e_date, multiplier=multiplier))
    log_activity(db, config.id, "Admin", "Add Season", name, f"x{multiplier}")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#seasons", status_code=303)

@app.post("/app/{extension}/admin/add_maintenance")
def add_maintenance(extension: str, unit_id: int = Form(...), start: str = Form(...), end: str = Form(...), reason: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if not unit or unit.room_type.site_config_id != config.id: return "Invalid Unit"
    db.add(models.MaintenanceBlock(room_type_id=unit.room_type_id, room_unit_id=unit.id, start_date=datetime.strptime(start, "%Y-%m-%d").date(), end_date=datetime.strptime(end, "%Y-%m-%d").date(), reason=reason, qty_blocked=1))
    log_activity(db, config.id, "Admin", "Block Unit", unit.label, reason)
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#maintenance", status_code=303)

@app.get("/app/{extension}/admin/edit_booking/{booking_id}", response_class=HTMLResponse)
def edit_booking_page(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    if not booking: return "Not found"
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == booking.room_type_id).all()
    balance = booking.total_price - booking.deposit_amount
    return templates.TemplateResponse("edit_booking.html", {"request": request, "config": config, "booking": booking, "rooms": rooms, "units": units, "balance": balance})

@app.post("/app/{extension}/admin/edit_booking/{booking_id}")
def edit_booking_save(request: Request, extension: str, booking_id: int, guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), room_unit_id: int = Form(...), status: str = Form(...), deposit: float = Form(0.0), notes: str = Form(""), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0); c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
    changes = []
    if booking.status != status: changes.append(f"Status: {booking.status}->{status}")
    if booking.check_in != c_in or booking.check_out != c_out or booking.room_type_id != booking.room_type_id or booking.rooms_booked != booking.rooms_booked:
        new_total = calculate_price(db, config.id, booking.room_type_id, c_in, c_out, booking.rooms_booked)
        booking.total_price = new_total
    booking.guest_name = guest_name; booking.guest_email = guest_email; booking.guest_phone = guest_phone
    booking.check_in = c_in; booking.check_out = c_out
    booking.room_unit_id = room_unit_id; booking.status = status; booking.deposit_amount = deposit; booking.notes = notes
    if changes: log_activity(db, config.id, context['user'].username, "Update Booking", booking.booking_code, ", ".join(changes))
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#bookings", status_code=303)

@app.get("/app/{extension}/admin/new_booking", response_class=HTMLResponse)
def new_booking_page(request: Request, extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    return templates.TemplateResponse("create_booking.html", {"request": request, "config": config, "rooms": rooms, "units": units})

@app.post("/app/{extension}/admin/new_booking")
async def new_booking_save(request: Request, extension: str, guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), room_id: int = Form(...), room_unit_id: Optional[int] = Form(None), status: str = Form(...), deposit: float = Form(0.0), notes: str = Form(""), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0); c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
    final_unit_id = room_unit_id
    if not final_unit_id:
        all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).all()
        for u in all_units:
            conflict = db.query(models.Booking).filter(models.Booking.room_unit_id == u.id, models.Booking.check_in < c_out, models.Booking.check_out > c_in).first()
            if not conflict: final_unit_id = u.id; break
    if not final_unit_id: return RedirectResponse(f"/app/{extension}/admin?error=No+Unit+Available", status_code=303)
    total = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
    new_booking = models.Booking(site_config_id=config.id, room_type_id=room_id, room_unit_id=final_unit_id, booking_code=b_code, guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone, check_in=c_in, check_out=c_out, status=status, total_price=total, deposit_amount=deposit, rooms_booked=1, notes=notes, created_at=datetime.now())
    db.add(new_booking); log_activity(db, config.id, context['user'].username, "Create Booking", b_code, "Manual Admin Creation")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Booking+Created#bookings", status_code=303)

@app.post("/app/{extension}/admin/upload_hero")
async def upload_hero(extension: str, images: List[UploadFile] = File(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    count = 0
    for img in images:
        if img.filename:
            path = f"static/uploads/hero_{extension}_{uuid.uuid4().hex[:6]}.jpg"
            await validate_and_save_image(img, path, "hero")
            db.add(models.HeroImage(site_config_id=context['config'].id, image_url=f"/{path}"))
            count += 1
    log_activity(db, context['config'].id, context['user'].username, "Upload Photos", "Hero Slider", f"Uploaded {count} images")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#hero", status_code=303)

@app.post("/app/{extension}/admin/delete_hero")
def delete_hero(extension: str, img_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.HeroImage).filter(models.HeroImage.id == img_id, models.HeroImage.site_config_id == context['config'].id).first()
    if img: db.delete(img); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#hero", status_code=303)

@app.post("/app/{extension}/admin/delete_season")
def delete_season(extension: str, season_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    s = db.query(models.SeasonalRate).filter(models.SeasonalRate.id == season_id, models.SeasonalRate.site_config_id == context['config'].id).first()
    if s: db.delete(s); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#seasons", status_code=303)

@app.post("/app/{extension}/admin/delete_maintenance")
def delete_maintenance(extension: str, block_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    b = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.MaintenanceBlock.id == block_id, models.RoomType.site_config_id == context['config'].id).first()
    if b: db.delete(b); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#maintenance", status_code=303)

@app.get("/app/{extension}/admin/invoice/{booking_id}", response_class=HTMLResponse)
def generate_invoice(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    subtotal = booking.total_price; tax = subtotal * 0.10; total = subtotal + tax; bal = total - booking.deposit_amount
    return templates.TemplateResponse("invoice.html", {"request": request, "config": config, "booking": booking, "subtotal": subtotal, "tax": tax, "total": total, "balance": bal, "now": datetime.now()})

@app.post("/app/{extension}/admin/delete_booking")
def delete_booking(request: Request, extension: str, booking_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == context['config'].id).first()
    if booking:
        log_activity(db, context['config'].id, context['user'].username, "Delete Booking", booking.booking_code, "Permanently Deleted")
        db.delete(booking); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#bookings", status_code=303)

@app.post("/app/{extension}/admin/update_cleaning_status")
def update_cleaning_status(extension: str, unit_id: int = Form(...), status: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if unit and unit.room_type.site_config_id == context['config'].id:
        unit.cleaning_status = status; db.commit()
    return RedirectResponse(f"/app/{extension}/admin#housekeeping", status_code=303)

@app.post("/app/{extension}/admin/change_password")
def change_password(extension: str, new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    user = context['user']; user.password_hash = pwd_context.hash(new_password); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Password+Changed#site", status_code=303)

@app.post("/app/{extension}/admin/change_staff_password")
def change_staff_password(extension: str, new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return RedirectResponse(f"/app/{extension}/admin?error=Unauthorized", status_code=303)
    config = context['config']
    staff_user = db.query(models.User).filter(models.User.site_config_id == config.id, models.User.role == 'staff').first()
    if staff_user:
        staff_user.password_hash = pwd_context.hash(new_password)
        log_activity(db, config.id, context['user'].username, "Security", "Staff Password", "Password changed by Admin")
        db.commit()
        return RedirectResponse(f"/app/{extension}/admin?success=Staff+Password+Updated#site", status_code=303)
    return RedirectResponse(f"/app/{extension}/admin?error=Staff+User+Not+Found#site", status_code=303)

@app.get("/app/{extension}/admin/edit_room/{room_id}", response_class=HTMLResponse)
def edit_room_page(request: Request, extension: str, room_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    current_labels = ", ".join([u.label for u in room.units])
    return templates.TemplateResponse("edit_room.html", {"request": request, "config": config, "room": room, "current_labels": current_labels})

@app.post("/app/{extension}/admin/edit_room/{room_id}")
async def edit_room_action(request: Request, extension: str, room_id: int, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(...), custom_labels: str = Form(""), new_images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    room.name = name; room.price_per_night = price; room.total_quantity = qty; room.description = desc; room.capacity = capacity
    
    # Sync Units
    raw_labels = [l.strip() for l in custom_labels.split(',') if l.strip()]
    existing_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room.id).order_by(models.RoomUnit.id).all()
    for i in range(qty):
        lbl = raw_labels[i] if i < len(raw_labels) else f"{name} #{i+1}"
        if i < len(existing_units): existing_units[i].label = lbl
        else: db.add(models.RoomUnit(room_type_id=room.id, label=lbl))
    # Remove excess
    if len(existing_units) > qty:
        for i in range(qty, len(existing_units)): db.delete(existing_units[i])
        
    if new_images:
        for img in new_images:
            if img.filename:
                path = f"static/uploads/room_{room.id}_{uuid.uuid4().hex[:6]}.jpg"
                await validate_and_save_image(img, path, "room")
                db.add(models.RoomImage(room_id=room.id, image_url=f"/{path}"))
    
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#rooms", status_code=303)

@app.post("/app/{extension}/admin/delete_room_image")
def delete_room_image(extension: str, img_id: int = Form(...), room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.RoomImage).filter(models.RoomImage.id == img_id).first()
    if img and img.room.site_config_id == context['config'].id: db.delete(img); db.commit()
    return RedirectResponse(f"/app/{extension}/admin/edit_room/{room_id}", status_code=303)

@app.post("/app/{extension}/admin/add_user")
def add_user(extension: str, username: str = Form(...), password: str = Form(...), role: str = Form("staff"), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    config = context['config']
    existing = db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first()
    if existing: return RedirectResponse(f"/app/{extension}/admin?error=User+Exists#users", status_code=303)
    new_user = models.User(site_config_id=config.id, username=username, password_hash=pwd_context.hash(password), role=role)
    db.add(new_user)
    log_activity(db, config.id, context['user'].username, "Create User", username, f"Role: {role}")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=User+Created#users", status_code=303)

@app.post("/app/{extension}/admin/delete_user")
def delete_user(extension: str, user_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_delete = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_delete:
        db.delete(user_to_delete)
        db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=User+Deleted#users", status_code=303)

@app.post("/app/{extension}/admin/update_user_password")
def update_user_password(extension: str, user_id: int = Form(...), new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_update = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_update:
        user_to_update.password_hash = pwd_context.hash(new_password)
        db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Password+Updated#users", status_code=303)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
