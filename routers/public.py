from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
import os

import models
from database import  get_db
from core import templates, get_config, calculate_price, limiter, get_current_time, log_activity
from sqlalchemy import func
from dotenv import load_dotenv

load_dotenv()

booking_limit = os.getenv("Booking_limit_per_minute", "5/minute")
search_limit = os.getenv("Search_limit_per_minute", "30/minute")

router = APIRouter()

def get_hotel_extension_from_request(request: Request) -> str:
    path_parts = request.url.path.split("/")
    if len(path_parts) > 2 and path_parts[1] == "app": return path_parts[2]
    return "unknown"

def get_rate_limit_key(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    ext = get_hotel_extension_from_request(request)
    return f"{ip}_{ext}"

def check_inventory_availability(db: Session, config_id: int, room_type_id: int, start_date: datetime, end_date: datetime, total_qty: int):
    curr = start_date
    min_availability = total_qty
    while curr < end_date:
        next_day = curr + timedelta(days=1)
        occupied_count = db.query(func.count(models.Booking.id)).filter(models.Booking.site_config_id == config_id, models.Booking.room_type_id == room_type_id, models.Booking.status.in_(['confirmed', 'pending', 'checked_in']), models.Booking.check_in < next_day, models.Booking.check_out > curr).scalar()
        blocked_count = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(models.MaintenanceBlock.room_type_id == room_type_id, models.MaintenanceBlock.start_date < next_day.date(), models.MaintenanceBlock.end_date > curr.date()).scalar() or 0
        available_tonight = total_qty - occupied_count - blocked_count
        if available_tonight <= 0: return False, 0
        if available_tonight < min_availability: min_availability = available_tonight
        curr = next_day
    return True, min_availability

def track_visitor(request: Request, config_id: int, db: Session):
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        ua = request.headers.get("user-agent", "unknown")
        path = request.url.path
        new_visit = models.Visitor(site_config_id=config_id, ip_address=ip, user_agent=ua, path=path, timestamp=get_current_time())
        db.add(new_visit); db.commit()
    except Exception: db.rollback()

@router.get("/app/{extension}")
@limiter.limit("30/minute", key_func=get_rate_limit_key)
def hotel_home(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    if not config.is_active: return templates.TemplateResponse("maintenance.html", {"request": request})
    track_visitor(request, config.id, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "logo_url": logo_url})

@router.post("/app/{extension}/search")
@limiter.limit(search_limit, key_func=get_rate_limit_key)
def hotel_search(request: Request, extension: str, check_in: str = Form(...), check_out: str = Form(...), guests: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    track_visitor(request, config.id, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None

    try: c_in = datetime.strptime(check_in, "%Y-%m-%d"); c_out = datetime.strptime(check_out, "%Y-%m-%d")
    except: return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Invalid dates.", "logo_url": logo_url})

    if c_out <= c_in: return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Check-out must be after check-in.", "logo_url": logo_url})
    
    max_days = config.max_booking_days if config.max_booking_days is not None else 10
    if (c_out - c_in).days > max_days:
        return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": f"Maximum booking length is {max_days} days.", "logo_url": logo_url})

    available_rooms = []
    for r in config.rooms:
        if r.capacity < guests: continue
        is_avail, count = check_inventory_availability(db, config.id, r.id, c_in, c_out, r.total_quantity)
        if is_avail:
            r.dynamic_total = calculate_price(db, config.id, r.id, c_in, c_out, 1)
            r.available_now = count
            available_rooms.append(r)
    return templates.TemplateResponse("search_results.html", {"request": request, "config": config, "rooms": available_rooms, "check_in": check_in, "check_out": check_out, "guests": guests, "logo_url": logo_url})

@router.get("/app/{extension}/book/{room_id}")
def book_page(request: Request, extension: str, room_id: int, check_in: Optional[str] = None, check_out: Optional[str] = None, guests: int = 1, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    if not check_in or not check_out:
        now = get_current_time()
        check_in = now.strftime("%Y-%m-%d"); check_out = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room, "prefill_check_in": check_in, "prefill_check_out": check_out, "prefill_guests": guests, "logo_url": logo_url})

@router.post("/app/{extension}/book/confirm")
@limiter.limit(booking_limit, key_func=get_rate_limit_key)
def book_confirm(request: Request, extension: str, room_id: int = Form(...), guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), guests_count: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    
    # 1. Basic Setup & Validation
    try:
        c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0)
        c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
    except ValueError:
        return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": db.query(models.RoomType).get(room_id), "error": "Invalid dates provided.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    max_rooms = config.max_rooms_per_booking if config.max_rooms_per_booking is not None else 2
    max_days = config.max_booking_days if config.max_booking_days is not None else 10

    # We need to fetch the room object partially for the error template if validation fails early
    # But we won't lock it yet.
    room_preview = db.query(models.RoomType).get(room_id)

    if rooms_needed > max_rooms:
        return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room_preview, "error": f"You can only book up to {max_rooms} rooms at once.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    if (c_out - c_in).days > max_days:
        return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room_preview, "error": f"Stay cannot exceed {max_days} days.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    # === START TRANSACTIONAL LOCKING ===
    try:
        # 2. Acquire Lock
        # We fetch the room_type using 'with_for_update()'. 
        # Any other request trying to book this room_id will pause here until we commit or rollback.
        room_type = db.query(models.RoomType).filter(models.RoomType.id == room_id).with_for_update().first()

        if not room_type:
             # Should practically never happen if ID is valid, but good safety
             raise Exception("Room type not found")

        # 3. Check Availability (Inside Lock)
        # Now that we hold the lock, this check is guaranteed to be accurate.
        is_avail, count = check_inventory_availability(db, config.id, room_id, c_in, c_out, room_type.total_quantity)
        
        if not is_avail or count < rooms_needed:
            db.rollback() # Release lock immediately
            return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room_type, "error": "Not enough rooms available for these dates (just booked by another guest).", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

        # 4. Smart Unit Assignment (Gap Logic)
        all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).all()
        assigned_units = []
        for _ in range(rooms_needed):
            best_unit = None; min_gap = float('inf'); candidates = []
            for u in all_units:
                if u in assigned_units: continue
                # Check for conflicts in this specific unit
                conflict = db.query(models.Booking).filter(models.Booking.room_unit_id == u.id, models.Booking.check_in < c_out, models.Booking.check_out > c_in, models.Booking.status.in_(['confirmed','pending','checked_in'])).first()
                maint = db.query(models.MaintenanceBlock).filter(models.MaintenanceBlock.room_unit_id == u.id, models.MaintenanceBlock.start_date < c_out.date(), models.MaintenanceBlock.end_date > c_in.date()).first()
                if not conflict and not maint: candidates.append(u)
            
            if not candidates: assigned_units.append(None); continue
            
            # Optimization: Find the unit that leaves the smallest gap between bookings
            for u in candidates:
                last_booking = db.query(models.Booking.check_out).filter(models.Booking.room_unit_id == u.id, models.Booking.check_out <= c_in, models.Booking.status.in_(['confirmed', 'checked_in', 'checked_out'])).order_by(models.Booking.check_out.desc()).first()
                gap = (c_in - last_booking[0]).total_seconds() if last_booking else 999999999.0
                if gap < min_gap: min_gap = gap; best_unit = u
            assigned_units.append(best_unit)

        # 5. Create Booking Records
        total_one = calculate_price(db, config.id, room_id, c_in, c_out, 1)
        total_all = total_one * rooms_needed; nights = (c_out - c_in).days
        created_bookings = []
        import uuid
        
        for i in range(rooms_needed):
            unit = assigned_units[i]; unit_id = unit.id if unit else None
            b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
            bk = models.Booking(
                site_config_id=config.id, 
                room_type_id=room_id, 
                room_unit_id=unit_id, 
                booking_code=b_code, 
                guest_name=guest_name, 
                guest_email=guest_email, 
                guest_phone=guest_phone, 
                check_in=c_in, 
                check_out=c_out, 
                total_price=total_one, 
                rooms_booked=1, 
                guests_count=guests_count, 
                created_at=get_current_time()
            )
            db.add(bk)
            created_bookings.append(bk)
            
            log_msg = f"{guest_name} booked {unit.label if unit else 'UNASSIGNED (Fragmentation)'}"
            log_activity(db, config.id, "Guest", "New Booking", b_code, log_msg)
        
        # 6. Commit Transaction
        # This saves the data AND releases the lock for the next user.
        db.commit()
        
        return templates.TemplateResponse("success.html", {"request": request, "config": config, "bookings": created_bookings, "total_cost": total_all, "nights": nights, "logo_url": logo_url})

    except Exception as e:
        db.rollback() # Crucial: Always rollback on error to release the lock
        print(f"Booking Error: {e}") # Replace with logger in production
        return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room_preview, "error": "An internal error occurred. Please try again.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})