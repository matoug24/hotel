from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
import os

import models
from database import get_db
# Import get_current_time from core
from core import templates, get_config, calculate_price, limiter, get_current_time, log_activity
from sqlalchemy import func

router = APIRouter()

# --- HELPER: CHECK INVENTORY AVAILABILITY ---
def check_inventory_availability(db: Session, config_id: int, room_type_id: int, start_date: datetime, end_date: datetime, total_qty: int):
    curr = start_date
    min_availability = total_qty
    
    while curr < end_date:
        next_day = curr + timedelta(days=1)
        
        occupied_count = db.query(func.count(models.Booking.id)).filter(
            models.Booking.site_config_id == config_id,
            models.Booking.room_type_id == room_type_id,
            models.Booking.status.in_(['confirmed', 'pending', 'checked_in']),
            models.Booking.check_in < next_day,
            models.Booking.check_out > curr
        ).scalar()
        
        blocked_count = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(
            models.MaintenanceBlock.room_type_id == room_type_id,
            models.MaintenanceBlock.start_date < next_day.date(),
            models.MaintenanceBlock.end_date > curr.date()
        ).scalar() or 0
        
        available_tonight = total_qty - occupied_count - blocked_count
        
        if available_tonight <= 0:
            return False, 0
        
        if available_tonight < min_availability:
            min_availability = available_tonight
            
        curr = next_day
        
    return True, min_availability

# --- HELPER: TRACK VISITOR ---
def track_visitor(request: Request, config_id: int, db: Session):
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        ua = request.headers.get("user-agent", "unknown")
        path = request.url.path
        
        new_visit = models.Visitor(
            site_config_id=config_id,
            ip_address=ip,
            user_agent=ua,
            path=path,
            timestamp=get_current_time() # USE LIBYA TIME
        )
        db.add(new_visit)
        db.commit()
    except Exception as e:
        db.rollback()

@router.get("/app/{extension}")
@limiter.limit("60/minute")
def hotel_home(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    if not config.is_active: return templates.TemplateResponse("maintenance.html", {"request": request})
    
    track_visitor(request, config.id, db)
    
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    
    return templates.TemplateResponse("index.html", {
        "request": request, "config": config, "rooms": config.rooms, 
        "hero_images": config.images, "logo_url": logo_url
    })

@router.post("/app/{extension}/search")
@limiter.limit("20/minute")
def hotel_search(request: Request, extension: str, check_in: str = Form(...), check_out: str = Form(...), guests: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    track_visitor(request, config.id, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None

    try:
        c_in = datetime.strptime(check_in, "%Y-%m-%d")
        c_out = datetime.strptime(check_out, "%Y-%m-%d")
    except:
        return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Invalid dates.", "logo_url": logo_url})

    if c_out <= c_in: return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Check-out must be after check-in.", "logo_url": logo_url})
    
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
        # Defaults also use Libya Time
        now = get_current_time()
        check_in = now.strftime("%Y-%m-%d")
        check_out = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        
    return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room, "prefill_check_in": check_in, "prefill_check_out": check_out, "prefill_guests": guests, "logo_url": logo_url})

@router.post("/app/{extension}/book/confirm")
@limiter.limit("5/minute")
def book_confirm(request: Request, extension: str, room_id: int = Form(...), guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), guests_count: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    
    c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0)
    c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
    
    room_type = db.query(models.RoomType).get(room_id)

    # 1. Inventory Check
    is_avail, count = check_inventory_availability(db, config.id, room_id, c_in, c_out, room_type.total_quantity)
    
    if not is_avail or count < rooms_needed:
        return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room_type, "error": "Not enough rooms available for these dates.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    # 2. Smart Assignment (Best Fit / Gap Minimization Strategy)
    all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).all()
    assigned_units = []
    
    for _ in range(rooms_needed):
        best_unit = None
        min_gap = float('inf') # Start with infinite gap
        
        candidates = []
        for u in all_units:
            if u in assigned_units: continue
            
            conflict = db.query(models.Booking).filter(
                models.Booking.room_unit_id == u.id,
                models.Booking.check_in < c_out, 
                models.Booking.check_out > c_in,
                models.Booking.status.in_(['confirmed','pending','checked_in'])
            ).first()
            
            maint = db.query(models.MaintenanceBlock).filter(
                models.MaintenanceBlock.room_unit_id == u.id,
                models.MaintenanceBlock.start_date < c_out.date(),
                models.MaintenanceBlock.end_date > c_in.date()
            ).first()
            
            if not conflict and not maint:
                candidates.append(u)
        
        if not candidates:
            assigned_units.append(None)
            continue

        for u in candidates:
            last_booking = db.query(models.Booking.check_out).filter(
                models.Booking.room_unit_id == u.id,
                models.Booking.check_out <= c_in,
                models.Booking.status.in_(['confirmed', 'checked_in', 'checked_out'])
            ).order_by(models.Booking.check_out.desc()).first()
            
            if last_booking:
                gap = (c_in - last_booking[0]).total_seconds()
            else:
                gap = 999999999.0
            
            if gap < min_gap:
                min_gap = gap
                best_unit = u
                
        assigned_units.append(best_unit)

    # 3. Create Bookings
    total_one = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    total_all = total_one * rooms_needed
    nights = (c_out - c_in).days
    
    created_bookings = []
    import uuid
    
    for i in range(rooms_needed):
        unit = assigned_units[i]
        unit_id = unit.id if unit else None
        
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
            created_at=get_current_time() # USE LIBYA TIME
        )
        db.add(bk)
        created_bookings.append(bk)
        
        log_msg = f"{guest_name} booked {unit.label if unit else 'UNASSIGNED (Fragmentation)'}"
        log_activity(db, config.id, "Guest", "New Booking", b_code, log_msg)
        
    db.commit()
    return templates.TemplateResponse("success.html", {"request": request, "config": config, "bookings": created_bookings, "total_cost": total_all, "nights": nights, "logo_url": logo_url})
