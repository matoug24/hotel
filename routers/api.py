from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, date

import models
from database import get_db
from fastapi.responses import JSONResponse
from core import calculate_price

router = APIRouter()

OCCUPYING_STATUSES = ("pending", "confirmed", "checked_in")

def _parse_yyyy_mm_dd(value: str) -> datetime:
    return datetime.strptime(value[:10], "%Y-%m-%d")

def _availability_for_room_type_on_day(
    db: Session,
    room_type: models.RoomType,
    day_start: datetime,
    day_end: datetime,
) -> int:
    """
    Returns remaining availability for a given room type for the night of [day_start, day_end).
    Uses end_date as exclusive for maintenance blocks.
    """
    booked_qty = (
        db.query(func.coalesce(func.sum(models.Booking.rooms_booked), 0))
        .filter(
            models.Booking.room_type_id == room_type.id,
            models.Booking.status.in_(OCCUPYING_STATUSES),
            models.Booking.check_in < day_end,
            models.Booking.check_out > day_start + timedelta(hours=12), # Added +12h buffer
        )
        .scalar()
        or 0
    )

    blocked_qty = (
        db.query(func.coalesce(func.sum(models.MaintenanceBlock.qty_blocked), 0))
        .filter(
            models.MaintenanceBlock.room_type_id == room_type.id,
            models.MaintenanceBlock.start_date <= day_start.date(),
            models.MaintenanceBlock.end_date > day_start.date(),  # end_date is exclusive
        )
        .scalar()
        or 0
    )

    remaining = int(room_type.total_quantity) - int(booked_qty) - int(blocked_qty)
    return max(remaining, 0)

@router.get("/{extension}/api/calendar_events")
def get_calendar_events_for_hotel(
    extension: str,
    start: str,
    end: str,
    room_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    config = db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first()
    if not config:
        raise HTTPException(status_code=404, detail="Hotel not found")

    start_dt = _parse_yyyy_mm_dd(start)
    end_dt = _parse_yyyy_mm_dd(end)

    # Do not show past days
    today_floor = datetime.combine(date.today(), datetime.min.time())
    if start_dt < today_floor:
        start_dt = today_floor

    if end_dt <= start_dt:
        return JSONResponse([])

    # Scope room types to this hotel
    room_types_q = db.query(models.RoomType).filter(models.RoomType.site_config_id == config.id)

    selected_room: Optional[models.RoomType] = None
    if room_id is not None:
        selected_room = room_types_q.filter(models.RoomType.id == room_id).first()
        if not selected_room:
            raise HTTPException(status_code=404, detail="Room type not found for this hotel")

    events: List[Dict[str, Any]] = []
    curr = start_dt

    while curr < end_dt:
        nxt = curr + timedelta(days=1)

        if selected_room is not None:
            remaining = _availability_for_room_type_on_day(db, selected_room, curr, nxt)

            events.append(
                {
                    "title": str(remaining) if remaining > 0 else "",
                    "start": curr.strftime("%Y-%m-%d"),
                    "allDay": True,
                    "backgroundColor": "#28a745" if remaining > 0 else "#dc3545",
                    "display": "background",
                }
            )
        else:
            # Hotel-wide: green if ANY room type has availability; red otherwise
            any_available = False
            for rt in room_types_q.all():
                if _availability_for_room_type_on_day(db, rt, curr, nxt) > 0:
                    any_available = True
                    break

            events.append(
                {
                    "title": "",
                    "start": curr.strftime("%Y-%m-%d"),
                    "allDay": True,
                    "backgroundColor": "#28a745" if any_available else "#dc3545",
                    "display": "background",
                }
            )

        curr = nxt

    return JSONResponse(events)


@router.post("/{extension}/api/calculate_price")
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
    except Exception as e: 
        return JSONResponse({"error": str(e)}, status_code=400)
