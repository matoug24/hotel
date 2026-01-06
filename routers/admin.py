import uuid
import os
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from datetime import datetime, timedelta

import models
from database import get_db
from core import templates, verify_hotel_admin, log_activity, calculate_price, validate_and_save_image, get_current_time, pwd_context
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

router = APIRouter()

# DUMMY ENDPOINT FOR LOGOUT CACHE POISONING
@router.get("/app/{extension}/admin/logout_bypass")
def logout_bypass(extension: str):
    return {"status": "logged_out"}

@router.get("/app/{extension}/admin", response_class=HTMLResponse)
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
    
    chart_labels = []; chart_data = []
    for i in range(14):
        day_t = today + timedelta(days=i); chart_labels.append(day_t.strftime("%b %d"))
        day_b = base_q.filter(func.date(models.Booking.check_in) == day_t, models.Booking.status == 'confirmed').all()
        chart_data.append(sum([b.total_price - b.deposit_amount for b in day_b]))
    
    logs = db.query(models.AuditLog).filter(models.AuditLog.site_config_id == config.id).order_by(models.AuditLog.timestamp.desc()).limit(100).all()
    
    total_capacity = sum([r.total_quantity for r in rooms])
    occupied = base_q.filter(models.Booking.check_in <= today, models.Booking.check_out > today, models.Booking.status.in_(['checked_in', 'confirmed'])).count()
    occupancy_rate = int((occupied / total_capacity * 100) if total_capacity > 0 else 0)
    
    return templates.TemplateResponse("admin.html", {
        "request": request, "config": config, "user": user, "hotel_users": hotel_users,
        "rooms": rooms, "all_units": all_units, "seasons": seasons, "blocks": blocks, "hero_images": config.images,
        "checkins_today_list": checkins_today, "checkouts_today_list": checkouts_today,
        "checkins_tomorrow_list": checkins_tmrw, "checkouts_tomorrow_list": checkouts_tmrw,
        "upcoming_bookings": upcoming, "active_bookings": active_bookings,
        "financials": {"future_deposits": round(future_dep, 2), "outstanding_balance": 0.0, "chart_labels": chart_labels, "chart_data": chart_data, "recent_bookings": []},
        "stats": {"revenue": round(revenue,2), "occupancy": occupancy_rate}, "logs": logs,
        "search_results": search_results, "search_query": search, "msg": request.query_params.get("success"), "err": request.query_params.get("error"), "sort_by": sort_by
    })

@router.get("/app/{extension}/admin/api/tape_chart")
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
            items.append({"id": b.id, "group": b.room_unit_id, "content": f"{b.guest_name}", "start": b.check_in.isoformat(), "end": b.check_out.isoformat(), "style": f"background-color: {color}; color: white; cursor: pointer; opacity: 0.7; border-radius: 6px;"})
    
    blocks = db.query(models.MaintenanceBlock).join(models.RoomType).filter(models.RoomType.site_config_id == config.id).all()
    for m in blocks:
        if m.room_unit_id: items.append({"id": f"maint_{m.id}", "group": m.room_unit_id, "content": "BLOCKED", "start": m.start_date.isoformat(), "end": m.end_date.isoformat(), "style": "background-color: black; opacity: 0.5; border-radius: 6px;", "type": "background"})
    return JSONResponse(content={"groups": groups, "items": items})

@router.post("/app/{extension}/admin/update_site")
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

@router.post("/app/{extension}/admin/add_user")
def add_user(extension: str, username: str = Form(...), password: str = Form(...), role: str = Form("staff"), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    config = context['config']
    if db.query(models.User).filter(models.User.username == username, models.User.site_config_id == config.id).first():
        return RedirectResponse(f"/app/{extension}/admin?error=User+Exists#users", status_code=303)
    new_user = models.User(site_config_id=config.id, username=username, password_hash=pwd_context.hash(password), role=role)
    db.add(new_user)
    log_activity(db, config.id, context['user'].username, "Create User", username, f"Role: {role}")
    db.commit()
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

@router.post("/app/{extension}/admin/delete_room")
def delete_room(extension: str, room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin': return "Unauthorized"
    
    room = db.query(models.RoomType).filter(models.RoomType.id == room_id, models.RoomType.site_config_id == context['config'].id).first()
    if not room: return RedirectResponse(f"/app/{extension}/admin?error=Room+Not+Found#rooms", status_code=303)

    # 1. Check active/future bookings
    today = get_current_time().date()
    active_booking = db.query(models.Booking).filter(
        models.Booking.room_type_id == room.id,
        models.Booking.check_out >= today,
        models.Booking.status.in_(['confirmed', 'checked_in', 'pending'])
    ).first()

    if active_booking:
        return RedirectResponse(f"/app/{extension}/admin?error=Cannot+Delete:+Active+Bookings+Exist#rooms", status_code=303)

    # 2. Cleanup Images from Disk
    for img in room.images:
        try:
            # image_url starts with /, remove it to get relative file path
            file_path = img.image_url.lstrip("/")
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error deleting file: {e}")

    # 3. Delete Record (Cascades should handle children in DB, but this is a clean delete of the parent)
    db.delete(room)
    log_activity(db, context['config'].id, context['user'].username, "Delete Room", room.name, "Room and assets deleted")
    db.commit()
    
    return RedirectResponse(f"/app/{extension}/admin?success=Room+Deleted#rooms", status_code=303)

@router.post("/app/{extension}/admin/add_season")
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

@router.post("/app/{extension}/admin/add_maintenance")
def add_maintenance(extension: str, unit_id: int = Form(...), start: str = Form(...), end: str = Form(...), reason: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    config = context['config']
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if not unit or unit.room_type.site_config_id != config.id: return "Invalid Unit"
    db.add(models.MaintenanceBlock(room_type_id=unit.room_type_id, room_unit_id=unit.id, start_date=datetime.strptime(start, "%Y-%m-%d").date(), end_date=datetime.strptime(end, "%Y-%m-%d").date(), reason=reason, qty_blocked=1))
    log_activity(db, config.id, "Admin", "Block Unit", unit.label, reason)
    db.commit()
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
    if not final_unit_id: return RedirectResponse(f"/app/{extension}/admin?error=No+Unit+Available", status_code=303)
    total = calculate_price(db, config.id, room_id, c_in, c_out, 1)
    b_code = f"RES-{uuid.uuid4().hex[:6].upper()}"
    new_booking = models.Booking(site_config_id=config.id, room_type_id=room_id, room_unit_id=final_unit_id, booking_code=b_code, guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone, check_in=c_in, check_out=c_out, status=status, total_price=total, deposit_amount=deposit, rooms_booked=1, notes=notes, created_at=datetime.now())
    db.add(new_booking); log_activity(db, config.id, context['user'].username, "Create Booking", b_code, "Manual Admin Creation")
    db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Booking+Created#bookings", status_code=303)

@router.post("/app/{extension}/admin/upload_hero")
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
    return templates.TemplateResponse("invoice.html", {"request": request, "config": config, "booking": booking, "subtotal": subtotal, "tax": tax, "total": total, "balance": bal, "now": datetime.now()})

@router.post("/app/{extension}/admin/delete_booking")
def delete_booking(request: Request, extension: str, booking_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id, models.Booking.site_config_id == context['config'].id).first()
    if booking:
        log_activity(db, context['config'].id, context['user'].username, "Delete Booking", booking.booking_code, "Permanently Deleted")
        db.delete(booking); db.commit()
    return RedirectResponse(f"/app/{extension}/admin#bookings", status_code=303)

@router.post("/app/{extension}/admin/update_cleaning_status")
def update_cleaning_status(extension: str, unit_id: int = Form(...), status: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    unit = db.query(models.RoomUnit).filter(models.RoomUnit.id == unit_id).first()
    if unit and unit.room_type.site_config_id == context['config'].id:
        unit.cleaning_status = status; db.commit()
    return RedirectResponse(f"/app/{extension}/admin#housekeeping", status_code=303)

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

@router.post("/app/{extension}/admin/delete_room_image")
def delete_room_image(extension: str, img_id: int = Form(...), room_id: int = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    img = db.query(models.RoomImage).filter(models.RoomImage.id == img_id).first()
    if img and img.room.site_config_id == context['config'].id: db.delete(img); db.commit()
    return RedirectResponse(f"/app/{extension}/admin/edit_room/{room_id}", status_code=303)

@router.post("/app/{extension}/admin/update_user_password")
def update_user_password(extension: str, user_id: int = Form(...), new_password: str = Form(...), context: dict = Depends(verify_hotel_admin), db: Session = Depends(get_db)):
    if context['user'].role != 'admin' and context['user'].role != 'owner': return "Unauthorized"
    user_to_update = db.query(models.User).filter(models.User.id == user_id, models.User.site_config_id == context['config'].id).first()
    if user_to_update:
        user_to_update.password_hash = pwd_context.hash(new_password)
        db.commit()
    return RedirectResponse(f"/app/{extension}/admin?success=Password+Updated#users", status_code=303)
