from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime, timedelta

import models
from database import get_db
from fastapi.responses import JSONResponse
from core import calculate_price

router = APIRouter()

@router.get("/api/calendar_events")
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

        # FIXED: Title is now empty string "" so no text appears on calendar
        if remaining > 0: 
            events.append({
                "title": "", 
                "start": curr.strftime("%Y-%m-%d"), 
                "allDay": True, 
                "backgroundColor": "#28a745", 
                "display": "background"
            })
        else: 
            events.append({
                "title": "", 
                "start": curr.strftime("%Y-%m-%d"), 
                "allDay": True, 
                "backgroundColor": "#dc3545", 
                "display": "background"
            })
        curr = nxt
    return JSONResponse(events)

@router.post("/app/{extension}/api/calculate_price")
def api_calculate_price(extension: str, room_id: int = Form(...), check_in: str = Form(...), check_out: str = Form(...), rooms_needed: int = Form(1), db: Session = Depends(get_db)):
    config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first()
    if not config: return JSONResponse({"error": "Hotel not found"}, status_code=404)
    try:
        c_in = datetime.strptime(check_in, "%Y-%m-%d").replace(hour=14, minute=0)
        c_out = datetime.strptime(check_out, "%Y-%m-%d").replace(hour=11, minute=0)
        if c_out <= c_in: return JSONResponse({"error": "Invalid dates"}, status_code=400)
        total = calculate_price(db, config.id, room_id, c_in, c_out, rooms_needed)
        nights = (c_out - c_in).days
        return JSONResponse({"total": total, "nights": nights})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=400)
