from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
import os

import models
from database import get_db
from core import templates, get_config, calculate_price, limiter
from sqlalchemy import func

router = APIRouter()

# --- TRACK VISITOR WITH LOGGING ---
def track_visitor(request: Request, config_id: int, db: Session):
    try:
        ip = request.client.host if request.client else "0.0.0.0"
        ua = request.headers.get("user-agent", "unknown")
        path = request.url.path
        
        # LOG TO CONSOLE
        print(f"--- VISITOR TRACKED: {ip} on {path} ---")
        
        new_visit = models.Visitor(
            site_config_id=config_id,
            ip_address=ip,
            user_agent=ua,
            path=path,
            timestamp=datetime.now()
        )
        db.add(new_visit)
        db.commit()
    except Exception as e:
        print(f"Tracking Error (Check DB Table): {e}")

@router.get("/app/{extension}")
@limiter.limit("60/minute")
def hotel_home(request: Request, extension: str, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    if not config.is_active: return templates.TemplateResponse("maintenance.html", {"request": request})
    
    # TRACK VISIT
    track_visitor(request, config.id, db)
    
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "config": config, 
        "rooms": config.rooms, 
        "hero_images": config.images,
        "logo_url": logo_url
    })

@router.post("/app/{extension}/search")
@limiter.limit("20/minute")
def hotel_search(request: Request, extension: str, check_in: str = Form(...), check_out: str = Form(...), guests: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    # TRACK SEARCH
    track_visitor(request, config.id, db)

    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None

    c_in = datetime.strptime(check_in, "%Y-%m-%d"); c_out = datetime.strptime(check_out, "%Y-%m-%d")
    if c_out <= c_in: return templates.TemplateResponse("index.html", {"request": request, "config": config, "rooms": config.rooms, "hero_images": config.images, "error": "Check-out must be after check-in.", "logo_url": logo_url})
    
    available_rooms = []
    for r in config.rooms:
        if r.capacity < guests: continue
        booked = db.query(func.count(models.Booking.id)).filter(models.Booking.room_type_id == r.id, models.Booking.status.in_(['confirmed', 'pending']), models.Booking.check_in < c_out, models.Booking.check_out > c_in).scalar()
        blocked = db.query(func.sum(models.MaintenanceBlock.qty_blocked)).filter(models.MaintenanceBlock.room_type_id == r.id, models.MaintenanceBlock.start_date < c_out.date(), models.MaintenanceBlock.end_date > c_in.date()).scalar() or 0
        if (r.total_quantity - booked - blocked) > 0:
            r.dynamic_total = calculate_price(db, config.id, r.id, c_in, c_out, 1)
            r.available_now = r.total_quantity - booked - blocked
            available_rooms.append(r)
    return templates.TemplateResponse("search_results.html", {"request": request, "config": config, "rooms": available_rooms, "check_in": check_in, "check_out": check_out, "guests": guests, "logo_url": logo_url})

@router.get("/app/{extension}/book/{room_id}")
def book_page(request: Request, extension: str, room_id: int, check_in: Optional[str] = None, check_out: Optional[str] = None, guests: int = 1, db: Session = Depends(get_db)):
    config = get_config(extension, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None

    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    if not check_in or not check_out: check_in = datetime.now().strftime("%Y-%m-%d"); check_out = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": room, "prefill_check_in": check_in, "prefill_check_out": check_out, "prefill_guests": guests, "logo_url": logo_url})

@router.post("/app/{extension}/book/confirm")
@limiter.limit("5/minute")
def book_confirm(request: Request, extension: str, room_id: int = Form(...), guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), guests_count: int = Form(1), db: Session = Depends(get_db)):
    config = get_config(extension, db)
    logo_path = f"static/uploads/{extension}_logo.png"
    logo_url = f"/{logo_path}" if os.path.exists(logo_path) else None

    c_in = datetime.strptime(check_in, "%Y-%m-%d"); c_out = datetime.strptime(check_out, "%Y-%m-%d")
    if c_out <= c_in: return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": db.query(models.RoomType).get(room_id), "error": "Invalid dates.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).order_by(models.RoomUnit.label).all()
    available_units = []
    for u in all_units:
        if len(available_units) >= rooms_needed: break
        conflict = db.query(models.Booking).filter(models.Booking.room_unit_id == u.id, models.Booking.check_in < c_out, models.Booking.check_out > c_in, models.Booking.status.in_(['confirmed','pending','checked_in'])).first()
        maint = db.query(models.MaintenanceBlock).filter(models.MaintenanceBlock.room_unit_id == u.id, models.MaintenanceBlock.start_date < c_out.date(), models.MaintenanceBlock.end_date > c_in.date()).first()
        if not conflict and not maint: available_units.append(u)

    if len(available_units) < rooms_needed: return templates.TemplateResponse("booking.html", {"request": request, "config": config, "room": db.query(models.RoomType).get(room_id), "error": "Not enough rooms available.", "prefill_check_in": check_in, "prefill_check_out": check_out, "logo_url": logo_url})

    total_one = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    total_all = total_one * rooms_needed
    nights = (c_out - c_in).days
    
    created_bookings = []
    import uuid
    for i in range(rooms_needed):
        b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
        bk = models.Booking(site_config_id=config.id, room_type_id=room_id, room_unit_id=available_units[i].id, booking_code=b_code, guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone, check_in=c_in, check_out=c_out, total_price=total_one, rooms_booked=1, guests_count=guests_count, created_at=datetime.now())
        db.add(bk); created_bookings.append(bk)
        from core import log_activity
        log_activity(db, config.id, "Guest", "New Booking", b_code, f"{guest_name} booked {available_units[i].label}")
    db.commit()
    return templates.TemplateResponse("success.html", {"request": request, "config": config, "bookings": created_bookings, "total_cost": total_all, "nights": nights, "logo_url": logo_url})
