import uuid
import os
import shutil
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from datetime import datetime, timedelta, timezone

import models
from database import get_db
from core import (templates, verify_hotel_admin, log_activity, calculate_price, validate_and_save_image, 
                  get_current_time, pwd_context, process_expired_bookings, verify_session)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

router = APIRouter()

@router.get("/{extension}/admin/logout_bypass")
def logout_bypass(extension: str): 
    return {"status": "logged_out"}

@router.get("/{extension}/admin", response_class=HTMLResponse)
def hotel_admin(request: Request, extension: str, sort_by: str = "check_in", search: Optional[str] = None, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']; user = context['user']
    process_expired_bookings(db, config.id)
    
    # Common Data
    hotel_users = db.query(models.User).filter(models.User.site_config_id == config.id).all()
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.id).all()
    logs = db.query(models.AuditLog).filter(models.AuditLog.site_config_id == config.id).order_by(models.AuditLog.timestamp.desc()).limit(500).all()
    
    today = get_current_time().date()
    base_q = db.query(models.Booking).filter(models.Booking.site_config_id == config.id)

    # --- BRANCH LOGIC: HALL vs HOTEL ---
    if config.site_type == 'hall':
        # HALL SPECIFIC DATA
        events_today = base_q.filter(
            func.date(models.Booking.check_in) == today,
            models.Booking.status == 'confirmed'
        ).all()
        
        upcoming = base_q.filter(
            func.date(models.Booking.check_in) >= today,
            models.Booking.status.in_(['confirmed'])
        ).order_by(models.Booking.check_in.asc()).limit(500).all()
        
        requests_q = base_q.filter(models.Booking.status == 'pending').order_by(models.Booking.created_at.desc()).all()
        
        # Financials (Simple Monthly)
        start_month = today.replace(day=1)
        # Simple revenue calc for this month
        revenue_month = db.query(func.sum(models.Booking.total_price)).filter(
            models.Booking.site_config_id == config.id,
            models.Booking.status == 'confirmed',
            models.Booking.check_in >= start_month
        ).scalar() or 0.0

        return templates.TemplateResponse("admin_hall.html", {
            "request": request, "config": config, "user": user, "hotel_users": hotel_users,
            "rooms": rooms, "hero_images": config.images,
            "events_today": events_today,
            "upcoming_bookings": upcoming,
            "request_bookings": requests_q,
            "new_requests_count": len(requests_q),
            "financials": {"revenue_month": round(revenue_month, 2)},
            "logs": logs,
            "msg": request.query_params.get("success"), "err": request.query_params.get("error")
        })

    # --- STANDARD HOTEL DATA ---
    all_units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.name, models.RoomUnit.label).all()
    seasons = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id).all()
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    
    tomorrow = today + timedelta(days=1)
    
    checkins_today = base_q.filter(func.date(models.Booking.check_in) == today, models.Booking.status != 'cancelled').all()
    checkouts_today = base_q.filter(func.date(models.Booking.check_out) == today, models.Booking.status != 'cancelled').all()
    checkins_tmrw = base_q.filter(func.date(models.Booking.check_in) == tomorrow, models.Booking.status != 'cancelled').all()
    checkouts_tmrw = base_q.filter(func.date(models.Booking.check_out) == tomorrow, models.Booking.status != 'cancelled').all()
    pending_count = base_q.filter(models.Booking.status == 'pending').count()
    
    upcoming = base_q.filter(
        func.date(models.Booking.check_in) >= today,
        models.Booking.status.in_(['confirmed', 'checked_in'])
    ).order_by(models.Booking.check_in.asc()).limit(500).all()
    
    requests_q = base_q.filter(models.Booking.status.in_(['pending', 'cancelled'])).order_by(models.Booking.created_at.desc()).limit(500).all()
    active_bookings = base_q.filter(models.Booking.status == 'checked_in').order_by(models.Booking.check_out.asc()).all()
    
    # Financials
    seven_days_future = today + timedelta(days=7)
    outstanding_q = db.query(models.Booking).filter(
        models.Booking.site_config_id == config.id,
        models.Booking.status.in_(['confirmed', 'checked_in']),
        func.date(models.Booking.check_out) >= today,
        func.date(models.Booking.check_out) <= seven_days_future
    ).all()
    outstanding_bal = sum([b.total_price - b.deposit_amount for b in outstanding_q])

    seven_days_ago = today - timedelta(days=7)
    revenue_7_q = db.query(models.Booking).filter(
        models.Booking.site_config_id == config.id,
        models.Booking.status.in_(['checked_out', 'checked_in', 'confirmed']),
        func.date(models.Booking.check_out) >= seven_days_ago,
        func.date(models.Booking.check_out) <= today
    ).all()
    revenue_7 = sum([b.total_price for b in revenue_7_q])

    todays_trans = base_q.filter(func.date(models.Booking.created_at) == today).order_by(models.Booking.created_at.desc()).all()

    # Chart Data
    chart_labels = []; chart_data = []
    start_date = today; end_date = today + timedelta(days=14)
    daily_revenue = db.query(func.date(models.Booking.check_in).label('day'), func.sum(models.Booking.total_price - models.Booking.deposit_amount).label('revenue')).filter(models.Booking.site_config_id == config.id, models.Booking.status == 'confirmed', models.Booking.check_in >= start_date, models.Booking.check_in < end_date).group_by(func.date(models.Booking.check_in)).all()
    revenue_map = {r.day: (r.revenue or 0) for r in daily_revenue}
    for i in range(14):
        current_day = start_date + timedelta(days=i)
        chart_labels.append(current_day.strftime("%b %d"))
        val = revenue_map.get(current_day, 0.0)
        chart_data.append(val)
        
    visitors = []
    try: visitors = db.query(models.Visitor).filter(models.Visitor.site_config_id == config.id).order_by(models.Visitor.timestamp.desc()).limit(1000).all()
    except: pass

    total_capacity = sum([r.total_quantity for r in rooms])
    occupied = base_q.filter(models.Booking.check_in <= today, models.Booking.check_out > today, models.Booking.status.in_(['checked_in', 'confirmed'])).count()
    occupancy_rate = int((occupied / total_capacity * 100) if total_capacity > 0 else 0)
    
    return templates.TemplateResponse("admin.html", {
        "request": request, "config": config, "user": user, "hotel_users": hotel_users,
        "rooms": rooms, "all_units": all_units, "seasons": seasons, "blocks": blocks, "hero_images": config.images,
        "checkins_today_list": checkins_today, "checkouts_today_list": checkouts_today,
        "checkins_tomorrow_list": checkins_tmrw, "checkouts_tomorrow_list": checkouts_tmrw,
        "upcoming_bookings": upcoming, 
        "request_bookings": requests_q,
        "active_bookings": active_bookings,
        "new_requests_count": pending_count,
        "financials": {"outstanding_balance": round(outstanding_bal, 2), "past_7_revenue": round(revenue_7, 2), "chart_labels": chart_labels, "chart_data": chart_data, "todays_transactions": todays_trans},
        "stats": {"occupancy": occupancy_rate}, 
        "logs": logs, "visitors": visitors,
        "search_results": [], "search_query": search, "msg": request.query_params.get("success"), "err": request.query_params.get("error"), "sort_by": sort_by
    })

# --- HALL SPECIFIC API FOR CALENDAR ---
@router.get("/{extension}/admin/api/hall_calendar")
def get_hall_calendar(extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    bookings = db.query(models.Booking).filter(
        models.Booking.site_config_id == config.id, 
        models.Booking.status.in_(['confirmed', 'pending'])
    ).all()
    
    events = []
    for b in bookings:
        color = '#28a745' if b.status == 'confirmed' else '#ffc107'
        # Check hour to determine if Morning or Evening for title
        # Morning starts at 9, Evening at 16
        is_morning = b.check_in.hour < 12
        title = f"{'☀️ Morning' if is_morning else '🌙 Evening'} - {b.guest_name}"
        
        events.append({
            "title": title,
            "start": b.check_in.isoformat(),
            "end": b.check_out.isoformat(),
            "backgroundColor": color,
            "url": f"/{extension}/admin/edit_booking/{b.id}"
        })
    return JSONResponse(events)

# --- BOOKING EDIT / CREATE ROUTING ---

@router.get("/{extension}/admin/new_booking", response_class=HTMLResponse)
def new_booking_page(request: Request, extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    
    if config.site_type == 'hall':
        return templates.TemplateResponse("create_booking_hall.html", {"request": request, "config": config, "rooms": rooms})
        
    units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    return templates.TemplateResponse("create_booking.html", {"request": request, "config": config, "rooms": rooms, "units": units})

@router.get("/{extension}/admin/edit_booking/{booking_id}", response_class=HTMLResponse)
def edit_booking_page(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    if not booking: return "Not found"
    
    if config.site_type == 'hall':
        rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
        # Determine session for pre-fill
        session_type = "Morning" if booking.check_in.hour < 12 else "Evening"
        return templates.TemplateResponse("edit_booking_hall.html", {
            "request": request, "config": config, "booking": booking, 
            "rooms": rooms, "balance": booking.total_price - booking.deposit_amount,
            "session_type": session_type
        })
        
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == booking.room_type_id).all()
    balance = booking.total_price - booking.deposit_amount
    return templates.TemplateResponse("edit_booking.html", {"request": request, "config": config, "booking": booking, "rooms": rooms, "units": units, "balance": balance})

# ... (Keep existing POST routes for edit_booking_save, new_booking_save, update_site, etc. They are generic enough to handle both if careful, or we update them if needed. 
# NOTE: The existing 'edit_booking_save' and 'new_booking_save' ALREADY handle logic generic enough, or I will update them below to be safe)

# SHARED/UPDATED POST ROUTES

@router.post("/{extension}/admin/new_booking")
async def new_booking_save(request: Request, extension: str, guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), 
                           check_in: Optional[str] = Form(None), check_out: Optional[str] = Form(None), # Hotel
                           booking_date: Optional[str] = Form(None), session_type: Optional[str] = Form(None), # Hall
                           room_id: int = Form(...), room_unit_id: Optional[int] = Form(None), status: str = Form(...), deposit: float = Form(0.0), notes: str = Form(""), 
                           context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    
    c_in = None
    c_out = None
    
    # HALL LOGIC
    if config.site_type == 'hall':
        if not booking_date or not session_type: return "Missing Hall Dates"
        b_date = datetime.strptime(booking_date, "%Y-%m-%d")
        if session_type == 'Morning':
            c_in = b_date.replace(hour=9, minute=0, second=0)
            c_out = b_date.replace(hour=14, minute=0, second=0)
        else:
            c_in = b_date.replace(hour=16, minute=0, second=0)
            c_out = b_date.replace(hour=23, minute=59, second=59)
        # Unit ID is usually automatic for halls (1 per room)
        unit = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).first()
        final_unit_id = unit.id if unit else None
        
    # HOTEL LOGIC
    else:
        try:
            if 'T' in check_in: c_in = datetime.strptime(check_in, "%Y-%m-%dT%H:%M")
            else: c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0)
            
            if 'T' in check_out: c_out = datetime.strptime(check_out, "%Y-%m-%dT%H:%M")
            else: c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
        except:
             return "Invalid Date Format"

        final_unit_id = room_unit_id
        if not final_unit_id:
            all_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room_id).all()
            for u in all_units:
                conflict = db.query(models.Booking).filter(models.Booking.room_unit_id == u.id, models.Booking.check_in < c_out, models.Booking.check_out > c_in).first()
                if not conflict: final_unit_id = u.id; break

    total = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
    new_booking = models.Booking(site_config_id=config.id, room_type_id=room_id, room_unit_id=final_unit_id, booking_code=b_code, guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone, check_in=c_in, check_out=c_out, status=status, total_price=total, deposit_amount=deposit, rooms_booked=1, notes=notes, created_at=get_current_time())
    db.add(new_booking); log_activity(db, config.id, context['user'].username, "Create Booking", b_code, "Manual Admin Creation"); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=Booking+Created#bookings", status_code=303)

# ... (Include other existing routes like delete_booking, upload_hero, etc. They are unchanged)
@router.post("/{extension}/admin/upload_hero")
async def upload_hero(extension: str, images: List[UploadFile] = File(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    count = 0
    for img in images:
        if img.filename:
            path = f"static/uploads/hero_{extension}_{uuid.uuid4().hex[:6]}.jpg"
            validate_and_save_image(img, path, "hero")
            db.add(models.HeroImage(site_config_id=context['config'].id, image_url=f"/{path}")); count += 1
    log_activity(db, context['config'].id, context['user'].username, "Upload Photos", "Hero Slider", f"Uploaded {count} images"); db.commit()
    return RedirectResponse(f"/{extension}/admin#hero", status_code=303)

@router.post("/{extension}/admin/delete_hero")
def delete_hero(extension: str, img_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.HeroImage).filter(models.HeroImage.id == img_id, models.HeroImage.site_config_id == context['config'].id).first()
    if img: db.delete(img); db.commit()
    return RedirectResponse(f"/{extension}/admin#hero", status_code=303)
# (Add all other existing routes here unchanged to ensure full file integrity)
@router.post("/{extension}/admin/update_site")
def update_site(extension: str, hotel_name: str = Form(...), highlights: str = Form(""), about_description: str = Form(""), amenities_list: str = Form(""), email: str = Form(""), phone: str = Form(""), address: str = Form(""), map_url: str = Form(""), facebook: str = Form(""), instagram: str = Form(""), youtube: str = Form(""), rules: str = Form(""), booking_success_message: str = Form(...), theme_id: int = Form(1), booking_expiration_hours: int = Form(24), is_active: Optional[str] = Form(None),  max_booking_days: int = Form(10), max_rooms_per_booking: int = Form(2),  db: Session = Depends(get_db), context: dict = Depends(verify_hotel_admin)):
    if context['user'].role != 'admin': return "Unauthorized"
    config = context['config']
    config.hotel_name = hotel_name; config.highlights = highlights; config.about_description = about_description; config.amenities_list = amenities_list; config.contact_email = email; config.contact_phone = phone; config.address = address; config.map_url = map_url; config.facebook = facebook; config.instagram = instagram; config.youtube = youtube; config.rules = rules; config.booking_success_message = booking_success_message; config.theme_id = theme_id; config.booking_expiration_hours = booking_expiration_hours
    config.max_booking_days = max_booking_days; config.max_rooms_per_booking = max_rooms_per_booking; 
    log_activity(db, config.id, context['user'].username, "Update Settings", "Site Config", "Settings updated"); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=Settings+Updated#site", status_code=303)

@router.post("/{extension}/admin/upload_logo")
async def upload_logo(extension: str, logo: UploadFile = File(...), context: dict = Depends(verify_hotel_admin)):
    file_path = f"static/uploads/{extension}_logo.png"
    with open(file_path, "wb") as buffer: shutil.copyfileobj(logo.file, buffer)
    return RedirectResponse(f"/{extension}/admin?success=Logo+Updated#hero", status_code=303)

@router.post("/{extension}/admin/delete_logo")
def delete_logo(extension: str, context: dict = Depends(verify_hotel_admin)):
    file_path = f"static/uploads/{extension}_logo.png"
    if os.path.exists(file_path): os.remove(file_path)
    return RedirectResponse(f"/{extension}/admin?success=Logo+Deleted#hero", status_code=303)

@router.post("/{extension}/admin/add_user")
def add_user(extension: str, username: str = Form(...), password: str = Form(...), role: str = Form("staff"), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    config = context['config']
    if db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first(): return RedirectResponse(f"/{extension}/admin?error=User+Exists#users", status_code=303)
    new_user = models.User(site_config_id=config.id, username=username, password_hash=pwd_context.hash(password), role=role)
    db.add(new_user); log_activity(db, config.id, context['user'].username, "Create User", username, f"Role: {role}"); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=User+Created#users", status_code=303)

@router.post("/{extension}/admin/delete_user")
def delete_user(extension: str, user_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_delete = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_delete: db.delete(user_to_delete); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=User+Deleted#users", status_code=303)

@router.post("/{extension}/admin/add_room")
async def add_room(extension: str, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(2), custom_labels: str = Form(""), images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    
    # Halls cannot add new rooms via UI (Locked to Morning/Evening)
    if config.site_type == 'hall':
         return RedirectResponse(f"/{extension}/admin?error=Cannot+add+rooms+in+Hall+mode#rooms", status_code=303)
         
    new_room = models.RoomType(site_config_id=config.id, name=name, price_per_night=price, total_quantity=qty, description=desc, capacity=capacity)
    db.add(new_room); db.commit()
    raw_labels = [l.strip() for l in custom_labels.split(',') if l.strip()]
    for i in range(qty): lbl = raw_labels[i] if i < len(raw_labels) else f"{name} #{i+1}"; db.add(models.RoomUnit(room_type_id=new_room.id, label=lbl))
    if images:
        for img in images:
            if img.filename: 
                path = f"static/uploads/room_{new_room.id}_{uuid.uuid4().hex[:6]}.jpg"
                validate_and_save_image(img, path, "room"); 
                db.add(models.RoomImage(room_id=new_room.id, image_url=f"/{path}"))
    log_activity(db, config.id, context['user'].username, "Create Room", name, f"Created with {qty} units"); db.commit()
    return RedirectResponse(f"/{extension}/admin#rooms", status_code=303)

@router.post("/{extension}/admin/delete_room")
def delete_room(extension: str, room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return "Unauthorized"
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == context['config'].id).first()
    if not room: return RedirectResponse(f"/{extension}/admin?error=Room+Not+Found#rooms", status_code=303)
    
    # System Locked check (for Halls)
    if room.is_system_locked:
         return RedirectResponse(f"/{extension}/admin?error=Cannot+Delete+System+Room#rooms", status_code=303)
    
    today = get_current_time().date()
    active_booking = db.query(models.Booking).filter(models.Booking.room_type_id == room.id, models.Booking.check_out >= today, models.Booking.status.in_(['confirmed', 'checked_in', 'pending'])).first()
    if active_booking: return RedirectResponse(f"/{extension}/admin?error=Cannot+Delete:+Active+Bookings+Exist#rooms", status_code=303)
    for img in room.images:
        try:
            file_path = img.image_url.lstrip("/"); 
            if os.path.exists(file_path): os.remove(file_path)
        except Exception: pass
    db.delete(room); log_activity(db, context['config'].id, context['user'].username, "Delete Room", room.name, "Room and assets deleted"); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=Room+Deleted#rooms", status_code=303)

@router.post("/{extension}/admin/toggle_room_active")
def toggle_room_active(extension: str, room_id: int = Form(...), is_active: bool = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return "Unauthorized"
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == context['config'].id).first()
    if room:
        room.is_active = is_active
        db.commit()
        return RedirectResponse(f"/{extension}/admin?success=Room+Updated#rooms", status_code=303)
    return RedirectResponse(f"/{extension}/admin?error=Room+Not+Found#rooms", status_code=303)

@router.post("/{extension}/admin/add_season")
def add_season(extension: str, name: str = Form(...), start: str = Form(...), end: str = Form(...), multiplier: float = Form(...), room_type_id: Optional[int] = Form(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    s_date = datetime.strptime(start, "%Y-%m-%d").date(); e_date = datetime.strptime(end, "%Y-%m-%d").date()
    if e_date <= s_date: return RedirectResponse(f"/{extension}/admin?error=End+Date+Must+Be+After+Start#seasons", status_code=303)
    conflict = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id, models.SeasonalRate.room_type_id == room_type_id, models.SeasonalRate.start_date <= e_date, models.SeasonalRate.end_date >= s_date).first()
    if conflict: return RedirectResponse(f"/{extension}/admin?error=Date+Overlap+With+{conflict.name}#seasons", status_code=303)
    db.add(models.SeasonalRate(site_config_id=config.id, room_type_id=room_type_id, name=name, start_date=s_date, end_date=e_date, multiplier=multiplier))
    log_activity(db, config.id, context['user'].username, "Add Season", name, f"x{multiplier}"); db.commit()
    return RedirectResponse(f"/{extension}/admin#seasons", status_code=303)

@router.post("/{extension}/admin/add_maintenance")
def add_maintenance(extension: str, unit_id: int = Form(...), start: str = Form(...), end: str = Form(...), reason: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if not unit or unit.room_type.site_config_id != config.id: return "Invalid Unit"
    db.add(models.MaintenanceBlock(room_type_id=unit.room_type_id, room_unit_id=unit.id, start_date=datetime.strptime(start, "%Y-%m-%d").date(), end_date=datetime.strptime(end, "%Y-%m-%d").date(), reason=reason, qty_blocked=1))
    log_activity(db, config.id, context['user'].username, "Block Unit", unit.label, reason); db.commit()
    return RedirectResponse(f"/{extension}/admin#maintenance", status_code=303)

@router.post("/{extension}/admin/edit_booking/{booking_id}")
def edit_booking_save(
    extension: str,
    booking_id: int,
    guest_name: str = Form(""),
    guest_email: str = Form(""),
    guest_phone: str = Form(""),
    check_in: Optional[str] = Form(None),
    check_out: Optional[str] = Form(None),
    room_unit_id: int = Form(0),
    status: str = Form(...),
    deposit: float = Form(0),
    notes: str = Form(""),

    # Hall Specific
    booking_date: Optional[str] = Form(None),
    session_type: Optional[str] = Form(None),

    room_type_id: Optional[int] = Form(None),
    rooms_booked: Optional[int] = Form(None),

    context=Depends(verify_hotel_admin),
    db: Session = Depends(get_db),
):
    config = context["config"]

    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Dates Handling
    c_in = booking.check_in
    c_out = booking.check_out
    
    # If Hall update
    if config.site_type == 'hall' and booking_date and session_type:
        b_date = datetime.strptime(booking_date, "%Y-%m-%d")
        if session_type == 'Morning':
            c_in = b_date.replace(hour=9, minute=0, second=0)
            c_out = b_date.replace(hour=14, minute=0, second=0)
        else:
            c_in = b_date.replace(hour=16, minute=0, second=0)
            c_out = b_date.replace(hour=23, minute=59, second=59)
    # If Hotel update
    elif check_in and check_out:
        try:
            if 'T' in check_in: c_in = datetime.strptime(check_in, "%Y-%m-%dT%H:%M")
            else: c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0)
            
            if 'T' in check_out: c_out = datetime.strptime(check_out, "%Y-%m-%dT%H:%M")
            else: c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid dates")

    if c_out <= c_in:
        raise HTTPException(status_code=400, detail="Invalid dates")

    new_room_type_id = booking.room_type_id if room_type_id is None else int(room_type_id)
    new_rooms_booked = booking.rooms_booked if rooms_booked is None else int(rooms_booked)

    if new_rooms_booked < 1:
        raise HTTPException(status_code=400, detail="rooms_booked must be >= 1")

    # Detect changes that should trigger repricing
    dates_changed = (c_in != booking.check_in) or (c_out != booking.check_out)
    room_changed = (new_room_type_id != booking.room_type_id)
    qty_changed = (new_rooms_booked != booking.rooms_booked)

    # Apply updates
    booking.guest_name = guest_name
    booking.guest_email = guest_email
    booking.guest_phone = guest_phone
    booking.check_in = c_in
    booking.check_out = c_out
    booking.status = status
    booking.deposit_amount = deposit
    booking.notes = notes

    booking.room_type_id = new_room_type_id
    booking.rooms_booked = new_rooms_booked

    if room_unit_id in (-1, 0):
        booking.room_unit_id = None
    else:
        booking.room_unit_id = room_unit_id

    if dates_changed or room_changed or qty_changed:
        booking.total_price = calculate_price(
            db,
            config.id,
            new_room_type_id,
            c_in,
            c_out,
            new_rooms_booked,
        )

    db.commit()
    return RedirectResponse(f"/{extension}/admin#bookings", status_code=303)

@router.get("/{extension}/admin/invoice/{booking_id}", response_class=HTMLResponse)
def generate_invoice(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    subtotal = booking.total_price; tax = subtotal * 0.10; total = subtotal + tax; bal = total - booking.deposit_amount
    return templates.TemplateResponse("invoice.html", {"request": request, "config": config, "booking": booking, "subtotal": subtotal, "tax": tax, "total": total, "balance": bal, "now": get_current_time()})

@router.post("/{extension}/admin/delete_booking")
def delete_booking(request: Request, extension: str, booking_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == context['config'].id).first()
    if booking:
        log_activity(db, context['config'].id, context['user'].username, "Delete Booking", booking.booking_code, "Permanently Deleted")
        db.delete(booking); db.commit()
    return RedirectResponse(f"/{extension}/admin#bookings", status_code=303)


@router.post("/{extension}/admin/change_password")
def change_password(extension: str, new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    user = context['user']; user.password_hash = pwd_context.hash(new_password); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=Password+Changed#site", status_code=303)

@router.post("/{extension}/admin/change_staff_password")
def change_staff_password(extension: str, new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return RedirectResponse(f"/{extension}/admin?error=Unauthorized", status_code=303)
    config = context['config']
    staff_user = db.query(models.User).filter(models.User.site_config_id == config.id, models.User.role == 'staff').first()
    if staff_user:
        staff_user.password_hash = pwd_context.hash(new_password)
        log_activity(db, config.id, context['user'].username, "Security", "Staff Password", "Password changed by Admin")
        db.commit()
        return RedirectResponse(f"/{extension}/admin?success=Staff+Password+Updated#site", status_code=303)
    return RedirectResponse(f"/{extension}/admin?error=Staff+User+Not+Found#site", status_code=303)

@router.get("/{extension}/admin/edit_room/{room_id}", response_class=HTMLResponse)
def edit_room_page(request: Request, extension: str, room_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    current_labels = ", ".join([u.label for u in room.units])
    return templates.TemplateResponse("edit_room.html", {"request": request, "config": config, "room": room, "current_labels": current_labels})

@router.post("/{extension}/admin/edit_room/{room_id}")
async def edit_room_action(request: Request, extension: str, room_id: int, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(...), custom_labels: str = Form(""), new_images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    room.name = name; room.price_per_night = price; room.description = desc; room.capacity = capacity
    
    # Hall: Prevent changing quantity to > 1
    if config.site_type == 'hall':
        room.total_quantity = 1
    else:
        room.total_quantity = qty
        
    raw_labels = [l.strip() for l in custom_labels.split(',') if l.strip()]
    existing_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room.id).order_by(models.RoomUnit.id).all()
    
    target_qty = 1 if config.site_type == 'hall' else qty
    
    for i in range(target_qty):
        lbl = raw_labels[i] if i < len(raw_labels) else f"{name} #{i+1}"
        if i < len(existing_units): existing_units[i].label = lbl
        else: db.add(models.RoomUnit(room_type_id=room.id, label=lbl))
    
    if len(existing_units) > target_qty:
        for i in range(target_qty, len(existing_units)): db.delete(existing_units[i])
        
    if new_images:
        for img in new_images:
            if img.filename:
                path = f"static/uploads/room_{room.id}_{uuid.uuid4().hex[:6]}.jpg"
                validate_and_save_image(img, path, "room")
                db.add(models.RoomImage(room_id=room.id, image_url=f"/{path}"))
    db.commit()
    return RedirectResponse(f"/{extension}/admin#rooms", status_code=303)

@router.post("/{extension}/admin/delete_room_image")
def delete_room_image(extension: str, img_id: int = Form(...), room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.RoomImage).filter(models.RoomImage.id == img_id).first()
    if img and img.room.site_config_id == context['config'].id: db.delete(img); db.commit()
    return RedirectResponse(f"/{extension}/admin/edit_room/{room_id}", status_code=303)

@router.post("/{extension}/admin/update_user_password")
def update_user_password(extension: str, user_id: int = Form(...), new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_update = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_update: user_to_update.password_hash = pwd_context.hash(new_password); db.commit()
    return RedirectResponse(f"/{extension}/admin?success=Password+Updated#users", status_code=303)