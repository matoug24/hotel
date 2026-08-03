from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse, HTMLResponse
import models
from database import get_db
from core import templates, verify_owner, log_activity, pwd_context, SECRET_KEY, ALGORITHM, OWNER_HASH
from jose import jwt, JWTError

router = APIRouter()

@router.get("/owner")
def owner_dashboard(request: Request, db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    configs = db.query(models.SiteConfig).all()
    msg = request.query_params.get("success")
    return templates.TemplateResponse("owner.html", {"request": request, "configs": configs, "msg": msg})

@router.post("/owner/create_site")
def create_site(extension: str = Form(...), name: str = Form(...), site_type: str = Form(...), admin_pass: str = Form(...), user_pass: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    if db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first(): 
        return RedirectResponse(url="/owner?error=Extension+exists", status_code=303)
    
    # Create Site Config
    new_conf = models.SiteConfig(extension=extension, hotel_name=name, site_type=site_type)
    db.add(new_conf)
    db.commit()
    
    # Create Users
    db.add_all([
        models.User(site_config_id=new_conf.id, username=f"{extension}_ad", password_hash=pwd_context.hash(admin_pass), role="admin"),
        models.User(site_config_id=new_conf.id, username=f"{extension}_user", password_hash=pwd_context.hash(user_pass), role="staff")
    ])
    
    # If Hall, create default locked rooms
    if site_type == 'hall':
        # Room 1: Morning Venue (Explicitly set is_active=True)
        r_m = models.RoomType(
            site_config_id=new_conf.id, 
            name="Morning Venue", 
            price_per_night=500.0, 
            total_quantity=1, 
            capacity=300, 
            description="Perfect for morning ceremonies.", 
            is_system_locked=True,
            is_active=True # FIX: Explicitly set to avoid Null constraint error
        )
        # Room 2: Evening Venue (Explicitly set is_active=True)
        r_e = models.RoomType(
            site_config_id=new_conf.id, 
            name="Evening Venue", 
            price_per_night=800.0, 
            total_quantity=1, 
            capacity=300, 
            description="Ideal for evening receptions.", 
            is_system_locked=True,
            is_active=True # FIX: Explicitly set to avoid Null constraint error
        )
        
        db.add(r_m)
        db.add(r_e)
        db.commit() # Commit to get IDs
        
        # Create Units (strictly 1 per room)
        db.add(models.RoomUnit(room_type_id=r_m.id, label="Main Hall"))
        db.add(models.RoomUnit(room_type_id=r_e.id, label="Main Hall"))
    
    db.commit()
    return RedirectResponse(url="/owner?success=Site+Created", status_code=303)

@router.post("/owner/reset_password")
def reset_hotel_password(config_id: int = Form(...), role: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    user = db.query(models.User).filter(models.User.site_config_id == config_id, models.User.role == role).first()
    if user: 
        user.password_hash = pwd_context.hash("ResetToday")
        log_activity(db, config_id, "Owner", "Password Reset", f"{role} User", "Reset")
        db.commit()
        return RedirectResponse(url="/owner?success=Password+Reset", status_code=303)
    return RedirectResponse(url="/owner?error=User+Not+Found", status_code=303)

@router.get("/logout")
def logout(request: Request, db=Depends(get_db)):
    token = request.cookies.get("access_token")
    extension = None

    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            config_id = payload.get("config_id")
            if config_id:
                cfg = db.query(models.SiteConfig).filter(models.SiteConfig.id == config_id).first()
                if cfg:
                    extension = cfg.extension
        except JWTError:
            pass

    target = f"/{extension}/login" if extension else "/"
    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie("access_token", path="/")
    return response

@router.post("/owner/delete_hotel")
def delete_hotel(config_id: int = Form(...), confirm_pass: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    if not pwd_context.verify(confirm_pass, OWNER_HASH):
        return RedirectResponse(url="/owner?error=Incorrect+Owner+Password", status_code=303)

    config = db.query(models.SiteConfig).filter(models.SiteConfig.id == config_id).first()
    if not config:
        return RedirectResponse(url="/owner?error=Site+Not+Found", status_code=303)

    try:
        name = config.hotel_name
        db.delete(config)
        db.commit()
        return RedirectResponse(url=f"/owner?success=Deleted+{name}", status_code=303)
    except Exception as e:
        db.rollback()
        return RedirectResponse(url="/owner?error=Delete+Failed", status_code=303)