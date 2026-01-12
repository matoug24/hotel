import uuid
import os
import shutil
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from datetime import datetime, timedelta, timezone

import models
from database import get_db
from core import (templates, verify_hotel_admin, log_activity, calculate_price, validate_and_save_image, 
                  get_current_time, pwd_context, process_expired_bookings)
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

router = APIRouter()

@router.get("/app/{extension}/admin/logout_bypass")
def logout_bypass(extension: str): 
    return {"status": "logged_out"}

@router.get("/app/{extension}/admin", response_class=HTMLResponse)
def hotel_admin(request: Request, extension: str, sort_by: str = "check_in", search: Optional[str] = None, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']; user = context['user']

    process_expired_bookings(db, config.id)
    
    # --- BASE DATA ---
    hotel_users = db.query(models.User).filter(models.User.site_config_id == config.id).all()
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    all_units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.name, models.RoomUnit.label).all()
    seasons = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id).all()
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    
    today = get_current_time().date()
    tomorrow = today + timedelta(days=1)
    base_q = db.query(models.Booking).filter(models.Booking.site_config_id == config.id)
    
    # --- 1. TAPE CHART / DASHBOARD STATS ---
    checkins_today = base_q.filter(func.date(models.Booking.check_in) == today, models.Booking.status != 'cancelled').all()
    checkouts_today = base_q.filter(func.date(models.Booking.check_out) == today, models.Booking.status != 'cancelled').all()
    checkins_tmrw = base_q.filter(func.date(models.Booking.check_in) == tomorrow, models.Booking.status != 'cancelled').all()
    checkouts_tmrw = base_q.filter(func.date(models.Booking.check_out) == tomorrow, models.Booking.status != 'cancelled').all()
    # NEW: Pending Requests Count
    pending_count = base_q.filter(models.Booking.status == 'pending').count()
    
    # --- 2. RESERVATIONS TAB (Confirmed Only) ---
    upcoming_q = base_q.filter(
        func.date(models.Booking.check_in) >= today,
        models.Booking.status.in_(['confirmed', 'checked_in']) # CHANGED: Only Confirmed/Active
    )
    upcoming = upcoming_q.order_by(models.Booking.check_in.asc()).limit(500).all()
    
    # --- 3. NEW REQUESTS TAB (Pending/Cancelled) ---
    requests_q = base_q.filter(models.Booking.status.in_(['pending', 'cancelled'])).order_by(models.Booking.created_at.desc()).limit(500).all()
    
    # --- 4. ACTIVE GUESTS ---
    active_bookings = base_q.filter(models.Booking.status == 'checked_in').order_by(models.Booking.check_out.asc()).all()
    
    # --- 5. FINANCIALS (7 Day Logic) ---
    # Outstanding (Next 7 Days): Balance of bookings checking out in [Today -> Today+7]
    seven_days_future = today + timedelta(days=7)
    outstanding_q = db.query(models.Booking).filter(
        models.Booking.site_config_id == config.id,
        models.Booking.status.in_(['confirmed', 'checked_in']),
        func.date(models.Booking.check_out) >= today,
        func.date(models.Booking.check_out) <= seven_days_future
    ).all()
    outstanding_bal = sum([b.total_price - b.deposit_amount for b in outstanding_q])

    # Revenue (Past 7 Days): Bookings checked out in [Today-7 -> Today]
    seven_days_ago = today - timedelta(days=7)
    revenue_7_q = db.query(models.Booking).filter(
        models.Booking.site_config_id == config.id,
        models.Booking.status.in_(['checked_out', 'checked_in', 'confirmed']),
        func.date(models.Booking.check_out) >= seven_days_ago,
        func.date(models.Booking.check_out) <= today
    ).all()
    revenue_7 = sum([b.total_price for b in revenue_7_q])

    # Today's Transactions (Created Today)
    todays_trans = base_q.filter(func.date(models.Booking.created_at) == today).order_by(models.Booking.created_at.desc()).all()

    # Chart Data (Forecast)
    # --- OPTIMIZED CHART DATA (N+1 Fix) ---
    # Instead of running 14 queries inside a loop, we run 1 query to get all data.
    chart_labels = []
    chart_data = []
    
    start_date = today
    end_date = today + timedelta(days=14)
    
    # 1. Single Aggregation Query
    # Groups results by day and sums the balance (Price - Deposit)
    daily_revenue = db.query(
        func.date(models.Booking.check_in).label('day'), 
        func.sum(models.Booking.total_price - models.Booking.deposit_amount).label('revenue')
    ).filter(
        models.Booking.site_config_id == config.id,
        models.Booking.status == 'confirmed',
        models.Booking.check_in >= start_date,
        models.Booking.check_in < end_date
    ).group_by(
        func.date(models.Booking.check_in)
    ).all()

    # 2. Convert to Dictionary for fast O(1) lookup
    # Format: { date(2023-10-01): 500.0, date(2023-10-02): 120.0 }
    revenue_map = {r.day: (r.revenue or 0) for r in daily_revenue}

    # 3. Build the Lists (Python-only loop, no DB calls)
    for i in range(14):
        current_day = start_date + timedelta(days=i)
        chart_labels.append(current_day.strftime("%b %d"))
        
        # Retrieve from map, default to 0 if no bookings that day
        val = revenue_map.get(current_day, 0.0)
        chart_data.append(val)

        
    # --- 6. LOGS & VISITORS ---
    logs = db.query(models.AuditLog).filter(models.AuditLog.site_config_id == config.id).order_by(models.AuditLog.timestamp.desc()).limit(500).all()
    visitors = []
    try: visitors = db.query(models.Visitor).filter(models.Visitor.site_config_id == config.id).order_by(models.Visitor.timestamp.desc()).limit(1000).all()
    except: pass

    # Occupancy
    total_capacity = sum([r.total_quantity for r in rooms])
    occupied = base_q.filter(models.Booking.check_in <= today, models.Booking.check_out > today, models.Booking.status.in_(['checked_in', 'confirmed'])).count()
    occupancy_rate = int((occupied / total_capacity * 100) if total_capacity > 0 else 0)
    
    return templates.TemplateResponse("admin.html", {
        "request": request, "config": config, "user": user, "hotel_users": hotel_users,
        "rooms": rooms, "all_units": all_units, "seasons": seasons, "blocks": blocks, "hero_images": config.images,
        "checkins_today_list": checkins_today, "checkouts_today_list": checkouts_today,
        "checkins_tomorrow_list": checkins_tmrw, "checkouts_tomorrow_list": checkouts_tmrw,
        # Upcoming & Requests Lists
        "upcoming_bookings": upcoming, 
        "request_bookings": requests_q, # NEW VARIABLE
        "active_bookings": active_bookings,
        "new_requests_count": pending_count, # NEW STAT
        "financials": {
            "outstanding_balance": round(outstanding_bal, 2), 
            "past_7_revenue": round(revenue_7, 2), # CHANGED VARIABLE NAME
            "chart_labels": chart_labels, 
            "chart_data": chart_data, 
            "todays_transactions": todays_trans # CHANGED LIST
        },
        "stats": {"occupancy": occupancy_rate}, 
        "logs": logs, "visitors": visitors,
        "search_results": [], "search_query": search, "msg": request.query_params.get("success"), "err": request.query_params.get("error"), "sort_by": sort_by
    })

@router.get("/app/{extension}/admin/api/tape_chart")
def get_tape_chart(extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).order_by(models.RoomType.name, models.RoomUnit.label).all()
    groups = []; groups.append({"id": "unassigned", "content": "<span style='color:red; font-weight:bold;'>⚠️ UNASSIGNED</span>", "style": "background-color: #ffe6e6;"})
    last_room_type_id = None
    for u in units:
        if last_room_type_id is not None and u.room_type_id != last_room_type_id: groups.append({"id": f"sep_{u.id}", "content": "", "style": "background-color: #e9ecef; height: 10px; border: none; pointer-events: none;", "className": "group-separator"})
        groups.append({"id": u.id, "content": f"<strong>{u.room_type.name}</strong> - {u.label}"}); last_room_type_id = u.room_type_id
    bookings = db.query(models.Booking).filter(models.Booking.site_config_id == config.id, models.Booking.status.in_(['confirmed', 'pending', 'checked_in', 'checked_out'])).all()
    items = []; center_style = "display: flex; align-items: center; justify-content: center; text-align: center;"
    for b in bookings:
        color = '#28a745'; font_color = 'white'
        if b.status == 'pending': color = '#ffc107'; font_color = 'black'
        elif b.status == 'checked_in': color = '#198754'
        elif b.status == 'checked_out': color = '#6c757d'
        group_id = b.room_unit_id if b.room_unit_id else "unassigned"
        style = f"background-color: {color}; color: {font_color}; border: 1px solid black; cursor: pointer; opacity: 0.9; border-radius: 4px; {center_style}"
        if group_id == "unassigned": style = f"background-color: #dc3545; color: white; border: 2px solid red; font-weight: bold; {center_style}"
        start_vis = b.check_in.strftime("%Y-%m-%d") + "T14:00:00"; end_vis = b.check_out.strftime("%Y-%m-%d") + "T10:00:00"
        items.append({"id": b.id, "group": group_id, "content": f"{b.guest_name}", "start": start_vis, "end": end_vis, "style": style})
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    for m in blocks:
        start_vis = m.start_date.strftime("%Y-%m-%d") + "T14:00:00"; end_vis = m.end_date.strftime("%Y-%m-%d") + "T10:00:00"
        style = f"background-color: #0d6efd; color: white; border: 1px solid black; opacity: 0.9; border-radius: 4px; {center_style}"
        if m.room_unit_id: items.append({"id": f"maint_{m.id}", "group": m.room_unit_id, "content": "BLOCKED", "start": start_vis, "end": end_vis, "style": style})
    return JSONResponse(content={"groups": groups, "items": items})

@router.post("/app/{extension}/admin/update_site")
def update_site(extension: str, hotel_name: str = Form(...), highlights: str = Form(""), about_description: str = Form(""), amenities_list: str = Form(""), email: str = Form(""), phone: str = Form(""), address: str = Form(""), map_url: str = Form(""), facebook: str = Form(""), instagram: str = Form(""), youtube: str = Form(""), rules: str = Form(""), booking_success_message: str = Form(...), theme_id: int = Form(1), booking_expiration_hours: int = Form(24), max_booking_days: int = Form(10), max_rooms_per_booking: int = Form(2),  db: Session = Depends(get_db), context: dict = Depends(verify_hotel_admin)):
    if context['user'].role != 'admin': return "Unauthorized"
    config = context['config']
    config.hotel_name = hotel_name; config.highlights = highlights; config.about_description = about_description; config.amenities_list = amenities_list; config.contact_email = email; config.contact_phone = phone; config.address = address; config.map_url = map_url; config.facebook = facebook; config.instagram = instagram; config.youtube = youtube; config.rules = rules; config.booking_success_message = booking_success_message; config.theme_id = theme_id; config.booking_expiration_hours = booking_expiration_hours
    config.max_booking_days = max_booking_days; config.max_rooms_per_booking = max_rooms_per_booking; 
    log_activity(db, config.id, context['user'].username, "Update Settings", "Site Config", "Settings updated"); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Settings+Updated#site", status_code=303)

@router.post("/app/{extension}/admin/upload_logo")
async def upload_logo(extension: str, logo: UploadFile = File(...), context: dict = Depends(verify_hotel_admin)):
    file_path = f"static/uploads/{extension}_logo.png"
    with open(file_path, "wb") as buffer: shutil.copyfileobj(logo.file, buffer)
    return RedirectResponse(f"/app/{extension}/admin?success=Logo+Updated#hero", status_code=303)

@router.post("/app/{extension}/admin/delete_logo")
def delete_logo(extension: str, context: dict = Depends(verify_hotel_admin)):
    file_path = f"static/uploads/{extension}_logo.png"
    if os.path.exists(file_path): os.remove(file_path)
    return RedirectResponse(f"/app/{extension}/admin?success=Logo+Deleted#hero", status_code=303)

@router.post("/app/{extension}/admin/add_user")
def add_user(extension: str, username: str = Form(...), password: str = Form(...), role: str = Form("staff"), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    config = context['config']
    if db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first(): return RedirectResponse(f"/app/{extension}/admin?error=User+Exists#users", status_code=303)
    new_user = models.User(site_config_id=config.id, username=username, password_hash=pwd_context.hash(password), role=role)
    db.add(new_user); log_activity(db, config.id, context['user'].username, "Create User", username, f"Role: {role}"); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=User+Created#users", status_code=303)

@router.post("/app/{extension}/admin/delete_user")
def delete_user(extension: str, user_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_delete = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_delete: db.delete(user_to_delete); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=User+Deleted#users", status_code=303)

@router.post("/app/{extension}/admin/add_room")
async def add_room(extension: str, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(2), custom_labels: str = Form(""), images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
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
    return RedirectResponse(f"/app/{extension}/admin#rooms", status_code=303)

@router.post("/app/{extension}/admin/delete_room")
def delete_room(extension: str, room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return "Unauthorized"
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == context['config'].id).first()
    if not room: return RedirectResponse(f"/app/{extension}/admin?error=Room+Not+Found#rooms", status_code=303)
    today = get_current_time().date()
    active_booking = db.query(models.Booking).filter(models.Booking.room_type_id == room.id, models.Booking.check_out >= today, models.Booking.status.in_(['confirmed', 'checked_in', 'pending'])).first()
    if active_booking: return RedirectResponse(f"/app/{extension}/admin?error=Cannot+Delete:+Active+Bookings+Exist#rooms", status_code=303)
    for img in room.images:
        try:
            file_path = img.image_url.lstrip("/"); 
            if os.path.exists(file_path): os.remove(file_path)
        except Exception: pass
    db.delete(room); log_activity(db, context['config'].id, context['user'].username, "Delete Room", room.name, "Room and assets deleted"); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Room+Deleted#rooms", status_code=303)

@router.post("/app/{extension}/admin/add_season")
def add_season(extension: str, name: str = Form(...), start: str = Form(...), end: str = Form(...), multiplier: float = Form(...), room_type_id: Optional[int] = Form(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    s_date = datetime.strptime(start, "%Y-%m-%d").date(); e_date = datetime.strptime(end, "%Y-%m-%d").date()
    if e_date <= s_date: return RedirectResponse(f"/app/{extension}/admin?error=End+Date+Must+Be+After+Start#seasons", status_code=303)
    conflict = db.query(models.SeasonalRate).filter(models.SeasonalRate.site_config_id == config.id, models.SeasonalRate.room_type_id == room_type_id, models.SeasonalRate.start_date <= e_date, models.SeasonalRate.end_date >= s_date).first()
    if conflict: return RedirectResponse(f"/app/{extension}/admin?error=Date+Overlap+With+{conflict.name}#seasons", status_code=303)
    db.add(models.SeasonalRate(site_config_id=config.id, room_type_id=room_type_id, name=name, start_date=s_date, end_date=e_date, multiplier=multiplier))
    log_activity(db, config.id, context['user'].username, "Add Season", name, f"x{multiplier}"); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#seasons", status_code=303)

@router.post("/app/{extension}/admin/add_maintenance")
def add_maintenance(extension: str, unit_id: int = Form(...), start: str = Form(...), end: str = Form(...), reason: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if not unit or unit.room_type.site_config_id != config.id: return "Invalid Unit"
    db.add(models.MaintenanceBlock(room_type_id=unit.room_type_id, room_unit_id=unit.id, start_date=datetime.strptime(start, "%Y-%m-%d").date(), end_date=datetime.strptime(end, "%Y-%m-%d").date(), reason=reason, qty_blocked=1))
    log_activity(db, config.id, context['user'].username, "Block Unit", unit.label, reason); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#maintenance", status_code=303)

@router.get("/app/{extension}/admin/edit_booking/{booking_id}", response_class=HTMLResponse)
def edit_booking_page(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    if not booking: return "Not found"
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == booking.room_type_id).all()
    balance = booking.total_price - booking.deposit_amount
    return templates.TemplateResponse("edit_booking.html", {"request": request, "config": config, "booking": booking, "rooms": rooms, "units": units, "balance": balance})

@router.post("/app/{extension}/admin/edit_booking/{booking_id}")
def edit_booking_save(request: Request, extension: str, booking_id: int, guest_name: str = Form(...), 
                      guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), 
                      check_in: str = Form(...), check_out: str = Form(...), room_unit_id: int = Form(...), 
                      status: str = Form(...), deposit: float = Form(0.0), notes: str = Form(""), 
                      context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0); c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
    changes = []
    if booking.status != status: 
        changes.append(f"Status: {booking.status}->{status}")

    if booking.check_in != c_in or booking.check_out != c_out or booking.room_type_id != booking.room_type_id or booking.rooms_booked != booking.rooms_booked:
        new_total = calculate_price(db, config.id, booking.room_type_id, c_in, c_out, booking.rooms_booked); 
        booking.total_price = new_total
    booking.guest_name = guest_name
    booking.guest_email = guest_email
    booking.guest_phone = guest_phone
    booking.check_in = c_in
    booking.check_out = c_out
    if room_unit_id == -1 or room_unit_id == 0: 
        booking.room_unit_id = None
    else: 
        booking.room_unit_id = room_unit_id
    booking.status = status; booking.deposit_amount = deposit; booking.notes = notes
    if changes: log_activity(db, config.id, context['user'].username, "Update Booking", booking.booking_code, ", ".join(changes))
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#bookings", status_code=303)

@router.get("/app/{extension}/admin/new_booking", response_class=HTMLResponse)
def new_booking_page(request: Request, extension: str, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    rooms = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    units = db.query(models.RoomUnit).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    return templates.TemplateResponse("create_booking.html", {"request": request, "config": config, "rooms": rooms, "units": units})

@router.post("/app/{extension}/admin/new_booking")
async def new_booking_save(request: Request, extension: str, guest_name: str = Form(...), guest_email: Optional[str] = Form(None), guest_phone: Optional[str] = Form(None), check_in: str = Form(...), check_out: str = Form(...), room_id: int = Form(...), room_unit_id: Optional[int] = Form(None), status: str = Form(...), deposit: float = Form(0.0), notes: str = Form(""), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0); c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
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
    return RedirectResponse(f"/app/{extension}/admin?success=Booking+Created#bookings", status_code=303)

@router.post("/app/{extension}/admin/upload_hero")
async def upload_hero(extension: str, images: List[UploadFile] = File(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    count = 0
    for img in images:
        if img.filename:
            path = f"static/uploads/hero_{extension}_{uuid.uuid4().hex[:6]}.jpg"
            validate_and_save_image(img, path, "hero")
            db.add(models.HeroImage(site_config_id=context['config'].id, image_url=f"/{path}")); count += 1
    log_activity(db, context['config'].id, context['user'].username, "Upload Photos", "Hero Slider", f"Uploaded {count} images"); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#hero", status_code=303)

@router.post("/app/{extension}/admin/delete_hero")
def delete_hero(extension: str, img_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.HeroImage).filter(models.HeroImage.id == img_id, models.HeroImage.site_config_id == context['config'].id).first()
    if img: db.delete(img); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#hero", status_code=303)

@router.post("/app/{extension}/admin/delete_season")
def delete_season(extension: str, season_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    s = db.query(models.SeasonalRate).filter(models.SeasonalRate.id == season_id, models.SeasonalRate.site_config_id == context['config'].id).first()
    if s: db.delete(s); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#seasons", status_code=303)

@router.post("/app/{extension}/admin/delete_maintenance")
def delete_maintenance(extension: str, block_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    b = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.MaintenanceBlock.id == block_id, models.RoomType.site_config_id == context['config'].id).first()
    if b: db.delete(b); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#maintenance", status_code=303)

@router.get("/app/{extension}/admin/invoice/{booking_id}", response_class=HTMLResponse)
def generate_invoice(request: Request, extension: str, booking_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == config.id).first()
    subtotal = booking.total_price; tax = subtotal * 0.10; total = subtotal + tax; bal = total - booking.deposit_amount
    return templates.TemplateResponse("invoice.html", {"request": request, "config": config, "booking": booking, "subtotal": subtotal, "tax": tax, "total": total, "balance": bal, "now": get_current_time()})

@router.post("/app/{extension}/admin/delete_booking")
def delete_booking(request: Request, extension: str, booking_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == context['config'].id).first()
    if booking:
        log_activity(db, context['config'].id, context['user'].username, "Delete Booking", booking.booking_code, "Permanently Deleted")
        db.delete(booking); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#bookings", status_code=303)


@router.post("/app/{extension}/admin/change_password")
def change_password(extension: str, new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    user = context['user']; user.password_hash = pwd_context.hash(new_password); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Password+Changed#site", status_code=303)

@router.post("/app/{extension}/admin/change_staff_password")
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

@router.get("/app/{extension}/admin/edit_room/{room_id}", response_class=HTMLResponse)
def edit_room_page(request: Request, extension: str, room_id: int, context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    current_labels = ", ".join([u.label for u in room.units])
    return templates.TemplateResponse("edit_room.html", {"request": request, "config": config, "room": room, "current_labels": current_labels})

@router.post("/app/{extension}/admin/edit_room/{room_id}")
async def edit_room_action(request: Request, extension: str, room_id: int, name: str = Form(...), price: float = Form(...), qty: int = Form(...), desc: str = Form(""), capacity: int = Form(...), custom_labels: str = Form(""), new_images: List[UploadFile] = File(None), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == config.id).first()
    room.name = name; room.price_per_night = price; room.total_quantity = qty; room.description = desc; room.capacity = capacity
    raw_labels = [l.strip() for l in custom_labels.split(',') if l.strip()]
    existing_units = db.query(models.RoomUnit).filter(models.RoomUnit.room_type_id == room.id).order_by(models.RoomUnit.id).all()
    for i in range(qty):
        lbl = raw_labels[i] if i < len(raw_labels) else f"{name} #{i+1}"
        if i < len(existing_units): existing_units[i].label = lbl
        else: db.add(models.RoomUnit(room_type_id=room.id, label=lbl))
    if len(existing_units) > qty:
        for i in range(qty, len(existing_units)): db.delete(existing_units[i])
    if new_images:
        for img in new_images:
            if img.filename:
                path = f"static/uploads/room_{room.id}_{uuid.uuid4().hex[:6]}.jpg"
                validate_and_save_image(img, path, "room")
                db.add(models.RoomImage(room_id=room.id, image_url=f"/{path}"))
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin#rooms", status_code=303)

@router.post("/app/{extension}/admin/delete_room_image")
def delete_room_image(extension: str, img_id: int = Form(...), room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.RoomImage).filter(models.RoomImage.id == img_id).first()
    if img and img.room.site_config_id == context['config'].id: db.delete(img); db.commit()
    return RedirectResponse(f"/app/{extension}/admin/edit_room/{room_id}", status_code=303)

@router.post("/app/{extension}/admin/update_user_password")
def update_user_password(extension: str, user_id: int = Form(...), new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_update = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_update: user_to_update.password_hash = pwd_context.hash(new_password); db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Password+Updated#users", status_code=303)
